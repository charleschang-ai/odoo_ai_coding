# -*- coding: utf-8 -*-
import http.client
import json
import logging
import socket
import xmlrpc.client
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Default field-name substrings whose VALUES are masked before data leaves for
# the LLM. Personal-contact fields (email/phone) are deliberately NOT in the
# default set — they are often the diagnostic clue — but can be added per host.
DEFAULT_SENSITIVE_PATTERNS = (
    "amount total price subtotal balance debit credit cost margin residual untaxed "
    "acc_number iban bank swift bic routing "
    "vat tax_id company_registry ssn passport id_number "
    "password api_key token secret "
    "salary wage payslip"
)
_MASK = "***"


def _parse_domain(raw):
    """Accept a domain as a list or a JSON string; return a list."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid domain (must be a JSON list): %s" % exc)
    if not isinstance(parsed, list):
        raise ValueError("domain must be a JSON list")
    return parsed


def _split_csv(raw):
    """Accept a comma-separated string (or list) of names; return a list."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in str(raw).replace('\n', ',').split(',')
            if part.strip()]


class _TimeoutTransport(xmlrpc.client.Transport):
    """xmlrpc Transport with a socket timeout so a slow/unreachable remote
    instance can never hang an Odoo worker indefinitely."""

    def __init__(self, timeout, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        chost, self._extra_headers, _x509 = self.get_host_info(host)
        return http.client.HTTPConnection(chost, timeout=self._timeout)


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS variant of :class:`_TimeoutTransport`."""

    def __init__(self, timeout, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        chost, self._extra_headers, _x509 = self.get_host_info(host)
        return http.client.HTTPSConnection(
            chost, timeout=self._timeout, context=getattr(self, 'context', None)
        )


class SupportRemoteInstance(models.Model):
    _name = 'support.remote.instance'
    _description = "Remote Odoo Instance (Support Target)"
    _order = 'name'

    # Methods the connector is allowed to call on the remote instance. Anything
    # that could mutate data (create/write/unlink/...) is intentionally absent:
    # this is defence-in-depth on top of the remote user's own access rights.
    _READONLY_METHODS = frozenset({
        'search', 'search_read', 'read', 'search_count', 'read_group',
        'fields_get', 'name_search', 'name_get', 'get_views', 'default_get',
        'web_search_read', 'web_read',
        'search_panel_select_range', 'search_panel_select_multi_range',
    })

    _MAX_LIMIT = 200
    _DEFAULT_LIMIT = 50

    name = fields.Char(
        string="Name", required=True,
        help="Label for this customer instance, e.g. 'Customer A — Production'.")
    active = fields.Boolean(default=True)
    color = fields.Integer(string="Color Index")

    url = fields.Char(
        string="URL", required=True,
        help="Base URL of the remote Odoo, e.g. https://customer.odoo.com")
    db = fields.Char(string="Database", required=True, help="Remote database name.")
    username = fields.Char(
        string="Username", required=True,
        help="Login (usually an email) of the dedicated read-only support user "
             "on the remote instance.")
    api_key = fields.Char(
        string="API Key",
        groups='ai_chatbot_enhance.group_ai_manager',
        help="API key generated from the remote user's profile "
             "(Preferences > Account Security). Stored per Odoo convention; "
             "restrict database access accordingly.")
    timeout = fields.Integer(
        string="Timeout (s)", default=20,
        help="Network timeout for each remote call, in seconds.")

    notes = fields.Text(string="Notes")

    # --- Sensitive-data masking (values replaced before data reaches the LLM) ---
    mask_sensitive = fields.Boolean(
        string="Mask Sensitive Data", default=True,
        help="When on, values of sensitive fields (amounts, bank/tax ids, "
             "credentials, ...) are replaced with *** before being sent to the "
             "AI. Turn off only when a diagnosis genuinely needs the real values.")
    mask_monetary = fields.Boolean(
        string="Mask All Monetary Fields", default=True,
        help="Also mask every field whose type is monetary, regardless of its "
             "name. Requires 'Mask Sensitive Data'.")
    sensitive_fields = fields.Text(
        string="Sensitive Field Keywords",
        default=lambda self: DEFAULT_SENSITIVE_PATTERNS,
        help="Space/comma/newline separated keywords. A field is masked when its "
             "technical name contains any keyword (case-insensitive).")

    # Populated by the connection test.
    version_info = fields.Char(string="Server Version", readonly=True)
    last_test = fields.Datetime(string="Last Tested", readonly=True)
    state = fields.Selection(
        [('unknown', 'Untested'), ('ok', 'Reachable'), ('fail', 'Failed')],
        string="Status", default='unknown', readonly=True)
    last_test_message = fields.Char(string="Last Test Message", readonly=True)

    session_count = fields.Integer(
        string="Diagnoses", compute='_compute_session_count')

    @api.depends('name')
    def _compute_session_count(self):
        data = self.env['support.diagnostic.session']._read_group(
            [('instance_id', 'in', self.ids)],
            groupby=['instance_id'], aggregates=['__count'],
        )
        mapped = {instance.id: count for instance, count in data}
        for record in self:
            record.session_count = mapped.get(record.id, 0)

    @api.constrains('timeout')
    def _check_timeout(self):
        for record in self:
            if record.timeout <= 0 or record.timeout > 300:
                raise ValidationError(_("Timeout must be between 1 and 300 seconds."))

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    def _normalized_url(self):
        self.ensure_one()
        url = (self.url or '').strip().rstrip('/')
        if not url:
            raise UserError(_("The remote URL is not set on %s.", self.display_name))
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url

    def _make_transport(self):
        self.ensure_one()
        timeout = self.timeout or self._DEFAULT_LIMIT
        scheme = urlsplit(self._normalized_url()).scheme
        if scheme == 'https':
            return _TimeoutSafeTransport(timeout)
        return _TimeoutTransport(timeout)

    def _server_proxy(self, endpoint):
        url = self._normalized_url()
        return xmlrpc.client.ServerProxy(
            '%s/xmlrpc/2/%s' % (url, endpoint),
            transport=self._make_transport(),
            allow_none=True,
        )

    def _authenticate(self):
        """Return the remote uid, raising a friendly error on failure."""
        self.ensure_one()
        api_key = self.sudo().api_key
        if not api_key:
            raise UserError(_("No API key configured for %s.", self.display_name))
        common = self._server_proxy('common')
        try:
            uid = common.authenticate(self.db, self.username, api_key, {})
        except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError,
                OSError, socket.timeout) as exc:
            raise UserError(_("Could not reach %s: %s", self.display_name, exc))
        if not uid:
            raise UserError(_(
                "Authentication failed on %s. Check the database name, "
                "username and API key.", self.display_name))
        return uid

    def open_session(self):
        """Authenticate once and return (uid, object_proxy) for reuse across
        several read calls within a single diagnosis."""
        self.ensure_one()
        uid = self._authenticate()
        return uid, self._server_proxy('object')

    def execute_ro(self, uid, object_proxy, model, method, args=None, kwargs=None):
        """Execute a whitelisted read-only method on the remote instance."""
        self.ensure_one()
        if method not in self._READONLY_METHODS:
            raise UserError(_(
                "Method '%s' is blocked: the support connector is read-only.",
                method))
        return object_proxy.execute_kw(
            self.db, uid, self.sudo().api_key,
            model, method, args or [], kwargs or {},
        )

    # ------------------------------------------------------------------
    # Sensitive-data masking
    # ------------------------------------------------------------------
    def _sensitive_tokens(self):
        self.ensure_one()
        raw = (self.sensitive_fields or '').replace(',', ' ').replace('\n', ' ')
        return [t.strip().lower() for t in raw.split() if t.strip()]

    def _fields_to_mask(self, uid, object_proxy, model, present_fields):
        """Names of fields (among present_fields) whose values must be masked."""
        self.ensure_one()
        if not self.mask_sensitive:
            return set()
        tokens = self._sensitive_tokens()
        masked = {f for f in present_fields
                  if any(tok in f.lower() for tok in tokens)}
        if self.mask_monetary:
            try:
                meta = self.execute_ro(uid, object_proxy, model, 'fields_get',
                                       [], {'attributes': ['type']})
                masked |= {f for f, m in meta.items()
                           if m.get('type') == 'monetary' and f in present_fields}
            except Exception as exc:  # noqa: BLE001 - masking must never break reads
                _logger.debug("monetary mask lookup failed on %s.%s: %s",
                              self.display_name, model, exc)
        return masked

    def _mask_rows(self, uid, object_proxy, model, rows):
        """Mask sensitive values in a list of record dicts (search_read output)."""
        self.ensure_one()
        if not self.mask_sensitive or not rows or not isinstance(rows, list):
            return rows
        present = set()
        for row in rows:
            if isinstance(row, dict):
                present |= set(row.keys())
        present.discard('id')
        to_mask = self._fields_to_mask(uid, object_proxy, model, present)
        if not to_mask:
            return rows
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in to_mask:
                if field in row and row[field] not in (False, None):
                    row[field] = _MASK
        return rows

    # ------------------------------------------------------------------
    # High-level masked read helpers (used by the AI tools)
    # ------------------------------------------------------------------
    def _clamp_limit(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return self._DEFAULT_LIMIT
        return self._DEFAULT_LIMIT if value <= 0 else min(value, self._MAX_LIMIT)

    def rpc_list_models(self, keyword=None):
        self.ensure_one()
        uid, proxy = self.open_session()
        domain = [('transient', '=', False)]
        if keyword:
            domain += ['|', ('model', 'ilike', keyword), ('name', 'ilike', keyword)]
        return self.execute_ro(uid, proxy, 'ir.model', 'search_read',
                               [domain], {'fields': ['model', 'name'], 'limit': 100})

    def rpc_fields_get(self, model):
        self.ensure_one()
        uid, proxy = self.open_session()
        return self.execute_ro(uid, proxy, model, 'fields_get', [],
                               {'attributes': ['string', 'type', 'relation',
                                               'required', 'selection']})

    def rpc_search_read(self, model, domain=None, fields_list=None,
                        limit=None, order=None):
        self.ensure_one()
        uid, proxy = self.open_session()
        kwargs = {'limit': self._clamp_limit(limit)}
        if fields_list:
            kwargs['fields'] = fields_list
        if order:
            kwargs['order'] = order
        rows = self.execute_ro(uid, proxy, model, 'search_read',
                               [domain or []], kwargs)
        return self._mask_rows(uid, proxy, model, rows)

    def rpc_read_group(self, model, domain=None, fields_list=None,
                       groupby=None, limit=None):
        self.ensure_one()
        uid, proxy = self.open_session()
        rows = self.execute_ro(uid, proxy, model, 'read_group',
                               [domain or [], fields_list or [], groupby or []],
                               {'limit': self._clamp_limit(limit)})
        return self._mask_rows(uid, proxy, model, rows)

    def rpc_count(self, model, domain=None):
        self.ensure_one()
        uid, proxy = self.open_session()
        return self.execute_ro(uid, proxy, model, 'search_count', [domain or []], {})

    def rpc_instance_info(self):
        self.ensure_one()
        return {
            'name': self.display_name,
            'url': self.url,
            'db': self.db,
            'version': self.version_info or 'unknown',
            'masking': 'on' if self.mask_sensitive else 'off',
        }

    # ------------------------------------------------------------------
    # Single entry point used by the AI server-action tools (returns JSON)
    # ------------------------------------------------------------------
    def ai_tool_run(self, kind, params=None):
        """Dispatch a read-only query for an AI tool and return a JSON string.
        Never raises: errors are returned as {"error": ...} so the LLM can react.
        """
        self.ensure_one()
        params = params or {}
        try:
            if kind == 'instance_info':
                data = self.rpc_instance_info()
            elif kind == 'list_models':
                data = self.rpc_list_models(params.get('keyword'))
            elif kind == 'fields_get':
                data = self.rpc_fields_get(self._req(params, 'model'))
            elif kind == 'search_read':
                data = self.rpc_search_read(
                    self._req(params, 'model'),
                    _parse_domain(params.get('domain')),
                    _split_csv(params.get('fields')),
                    params.get('limit'),
                    params.get('order') or None)
            elif kind == 'read_group':
                data = self.rpc_read_group(
                    self._req(params, 'model'),
                    _parse_domain(params.get('domain')),
                    _split_csv(params.get('fields')),
                    _split_csv(params.get('groupby')),
                    params.get('limit'))
            elif kind == 'count':
                data = {'count': self.rpc_count(self._req(params, 'model'),
                                                _parse_domain(params.get('domain')))}
            else:
                return json.dumps({'error': "unknown query kind '%s'" % kind},
                                  ensure_ascii=False)
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 - feed the error back to the LLM
            return json.dumps({'error': str(exc)}, ensure_ascii=False)

    @staticmethod
    def _req(params, key):
        value = params.get(key)
        if not value:
            raise ValueError("parameter '%s' is required" % key)
        return value

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        notif_type = 'success'
        try:
            common = self._server_proxy('common')
            version = common.version() or {}
            uid = self._authenticate()
            self.write({
                'version_info': version.get('server_version', _('unknown')),
                'last_test': fields.Datetime.now(),
                'state': 'ok',
                'last_test_message': _("Connected as uid %s.", uid),
            })
            message = _(
                "Connected to %(name)s. Server version %(ver)s, uid %(uid)s.",
                name=self.display_name,
                ver=version.get('server_version', _('unknown')),
                uid=uid,
            )
        except UserError as exc:
            notif_type = 'danger'
            self.write({
                'last_test': fields.Datetime.now(),
                'state': 'fail',
                'last_test_message': str(exc),
            })
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface any transport error
            _logger.warning("Support connection test failed for %s: %s",
                            self.display_name, exc)
            notif_type = 'danger'
            self.write({
                'last_test': fields.Datetime.now(),
                'state': 'fail',
                'last_test_message': str(exc),
            })
            message = _("Connection failed: %s", exc)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': notif_type,
                'title': _("Remote Connection"),
                'message': message,
                'sticky': notif_type == 'danger',
            },
        }

    def action_open_sessions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Diagnoses — %s", self.display_name),
            'res_model': 'support.diagnostic.session',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }
