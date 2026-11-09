from lxml import etree
from ast import literal_eval
from odoo import fields, models, api
from odoo.http import request
import logging
from odoo.tools.misc import mute_logger, submap
import json
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


def compute_report_measures(fields, field_attrs=None, active_measures=None, sum_aggregator_only=False):
    """
    Python equivalent of the JavaScript computeReportMeasures function.

    Args:
        fields (dict): Dictionary of field definitions from fields_get()
        field_attrs (dict): Dictionary of field attributes with visibility info
        active_measures (list): List of active measure field names
        sum_aggregator_only (bool): Only include fields with 'sum' aggregator

    Returns:
        dict: Ordered dictionary of measures with their field definitions
    """
    if field_attrs is None:
        field_attrs = {}
    if active_measures is None:
        active_measures = []

    # Start with the count measure
    measures = {"__count": {"name": "__count", "string": "Count", "type": "integer"}}

    # Process regular fields
    for field_name, field in fields.items():
        if field_name == "id":
            continue

        # Check if field is invisible
        field_attr = field_attrs.get(field_name, {})
        if field_attr.get("isInvisible", False):
            continue

        # Check if field is numeric and has aggregator
        if field.get("type") in ["integer", "float", "monetary"]:
            aggregator = field.get("aggregator")
            if aggregator:
                if sum_aggregator_only and aggregator != "sum":
                    continue
                # Filter field to only include the keys we want
                filtered_field = submap(field, ["type", "aggregator", "name", "string", "sortable"])
                measures[field_name] = filtered_field

    # Add active measures to the measure list
    # This is rarely necessary, but can be useful for functional fields
    # with overridden read_group methods
    for measure in active_measures:
        if measure not in measures and measure in fields:
            # Filter field to only include the keys we want
            filtered_field = submap(fields[measure], ["type", "aggregator", "name", "string", "sortable"])
            measures[measure] = filtered_field

    # Override field strings from field_attrs if provided
    for field_name, field_attr in field_attrs.items():
        if field_attr.get("string") and field_name in measures:
            measures[field_name] = dict(measures[field_name])
            measures[field_name]["string"] = field_attr["string"]

    # Sort measures: Count is always last, others alphabetically by string
    def sort_key(item):
        field_name, field_def = item
        if field_name == "__count":
            return 1, ""  # Count goes last
        return 0, field_def.get("string", "").lower()

    sorted_measures = sorted(measures.items(), key=sort_key)
    return dict(sorted_measures)


def validate_measures(model, measures):
    fields = model.fields_get()
    valid_measures_dict = compute_report_measures(fields, None)
    for measure in measures:
        parts = measure.strip().split()
        if ':' in parts[0]:
            raise ValueError(
                f"Invalid measure syntax '{measure}' for model '{model}'. "
                "Aggregation operators like ':sum' are not supported. "
                "Use '<field_name>' or '<field_name> asc/desc' instead."
            )
        base_measure = parts[0]
        if base_measure not in valid_measures_dict:
            raise ValueError(
                f"Measure '{base_measure}' is invalid for model '{model}'. "
                f"The base field is not a recognized or aggregatable field."
            )


def clean_search_view_xml(search_view_arch):
    """Clean and restructure search view XML for AI consumption."""
    if not search_view_arch:
        return ""

    # Parse XML
    tree = etree.fromstring(search_view_arch)

    # Create new clean structure
    clean_tree = etree.Element("search")

    # 1. Add searchable fields (excluding only those with invisible="1")
    searchable_fields_elem = etree.SubElement(clean_tree, "searchable_fields")
    for field in tree.xpath(".//field[@name and not(@invisible='1') and not(ancestor::group)]"):
        # Copy only essential attributes
        clean_field = etree.SubElement(searchable_fields_elem, "field")
        for attr in ["name", "string", "filter_domain", "operator"]:
            if field.get(attr):
                clean_field.set(attr, field.get(attr))

    # 2. Add filters grouped by separators (excluding those with invisible="1")
    filters_elem = etree.SubElement(clean_tree, "filters")

    # Process filters in groups separated by separators
    current_group = None
    for elem in tree:
        if elem.tag == "separator":
            # Start a new group on separator
            current_group = None
        elif elem.tag == "filter" and elem.get("name") and elem.get("invisible") != "1":
            # Skip filters that are inside <group> elements (those are groupbys)
            if elem.getparent().tag != "group":
                if current_group is None:
                    current_group = etree.SubElement(filters_elem, "group")
                clean_filter = etree.SubElement(current_group, "filter")
                for attr in ["name", "string", "domain", "date"]:
                    if elem.get(attr):
                        clean_filter.set(attr, elem.get(attr))

    # 3. Add groupby filters with extracted field information
    groupbys_elem = etree.SubElement(clean_tree, "groupbys")
    for group in tree.xpath(".//group"):
        for filter_elem in group.xpath(".//filter[@name and not(@invisible='1')]"):
            if filter_elem.get("context") and "group_by" in filter_elem.get("context"):
                clean_filter = etree.SubElement(groupbys_elem, "filter")
                clean_filter.set("name", filter_elem.get("name"))
                if filter_elem.get("string"):
                    clean_filter.set("string", filter_elem.get("string"))

                # Extract the actual field name from the context
                context_str = filter_elem.get("context")
                context_dict = literal_eval(context_str)
                if "group_by" in context_dict:
                    clean_filter.set("group_by_field", context_dict["group_by"])

    # Return compact XML string
    return etree.tostring(clean_tree, encoding="unicode", pretty_print=False)


def validate_search_terms(search_terms):
    if not search_terms:
        return

    invalid_search_terms = []
    for search_term in search_terms:
        field, __ = search_term.split("=")
        if len(field.split(".")) > 1:
            invalid_search_terms.append(search_term)

    if invalid_search_terms:
        raise ValueError(f"Search terms with field chains are not allowed: {invalid_search_terms}")


def validate_groupbys(model, groupbys):
    if not groupbys:
        return

    model_fields = model.fields_get()
    invalid_groupbys = []
    for groupby in groupbys:
        if len(groupby.split(".")) > 1 or not model_fields.get(groupby, {}).get('groupable'):
            invalid_groupbys.append(groupby)

    if invalid_groupbys:
        raise ValueError(f"The following groupby values are not allowed: {invalid_groupbys}")


class AIAgentAdvanced(models.Model):
    _inherit = 'ai.agent.bot'

    def _ai_tool_get_menu_details(self, menu_ids):
        if not isinstance(menu_ids, list):
            raise TypeError("menu_ids must be a list of menu IDs.")

        if not menu_ids:
            raise ValueError("At least one menu ID must be provided.")

        # Load all menus to validate IDs
        menus = self.env["ir.ui.menu"].load_menus(False)

        csv_result = "menu_id|model|context|domain|search_view\n"

        for menu_id in menu_ids:
            if not isinstance(menu_id, (int, float)):
                csv_result += f"{menu_id}|Error: Menu ID must be a number|\n"
                continue

            menu_id = int(menu_id)
            menu = menus.get(menu_id)

            if not menu:
                csv_result += f"{menu_id}|Error: Menu not found|\n"
                continue

            action = self.env["ir.actions.act_window"].browse(menu["action_id"])
            if not action.exists():
                csv_result += f"{menu_id}|Error: Action not found|\n"
                continue

            # Get context and domain
            context_str = str(action.context or {})
            domain_str = str(action.domain or [])

            search_view = self.env[action.res_model].get_view(action.search_view_id.id, 'search')
            search_view_xml = clean_search_view_xml(search_view['arch']) if search_view else ""

            # Escape the XML for CSV - replace quotes and newlines
            if search_view_xml:
                search_view_xml = search_view_xml.replace('\n', ' ').replace('\r', '')

            csv_result += (
                f"{menu_id}|"
                f"{action.res_model}|"
                f"{context_str}|"
                f"{domain_str}|"
                f"{search_view_xml}\n"
            )

        return csv_result.strip()

    def _ai_tool_get_fields(self, model_name, include_description=True):
        if not isinstance(model_name, str):
            raise TypeError("Model name must be a string.")

        if not model_name:
            raise ValueError("Model name must be provided.")

        if model_name not in self.env:
            raise ValueError(f"Model '{model_name}' not found.")

        model = self.env[model_name]
        model_fields = model.fields_get()
        results = []

        # Add header
        if include_description:
            results.append("field_name|display_name|type|sortable|groupable|description")
        else:
            results.append("field_name|display_name|type|sortable|groupable")

        for field_name, field_info in model_fields.items():
            if not model._fields[field_name]._description_searchable:
                continue
            field_type = field_info.get('type', 'unknown')
            field_relation = field_info.get('relation', '')
            field_display_name = field_info.get('string', '')
            sortable = str(field_info.get('sortable', False)).lower()
            groupable = str(field_info.get('groupable', False)).lower()
            if field_relation:
                field_type += f"({field_relation})"
            if field_type == 'selection':
                selection_items = field_info.get('selection', [])
                field_type += f"({dict(selection_items)})"
            # Format as CSV with pipe delimiter: field_name|display_name|type|sortable|groupable|description
            field_str = f"{field_name}|{field_display_name}|{field_type}|{sortable}|{groupable}"
            if include_description:
                if description := field_info.get('help', ''):
                    # Replace any pipe characters in the description to avoid delimiter conflicts
                    safe_description = description.replace('|', '&#124;')
                    field_str += f"|{safe_description}"
                else:
                    field_str += "|"  # Empty description column for consistent format
            results.append(field_str)

        return "\n".join(results)

    @api.model
    def _parse_domain(self, model_name, domain_json_str: str | None):
        if not domain_json_str or not domain_json_str.strip():
            return None

        try:
            domain_array = json.loads(domain_json_str)
            Domain(domain_array).optimize_full(self.env[model_name])
            return domain_array
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format for custom domain: {e}")
        except ValueError as e:
            raise ValueError(f"Invalid custom domain for model '{model_name}': {e}")

    def _ai_tool_open_menu_list(self, menu_id, model_name, selected_filters, selected_groupbys, search,
                                custom_domain=None):
        validate_search_terms(search)
        validate_groupbys(self.env[model_name], selected_groupbys)
        menus = self.env["ir.ui.menu"].load_web_menus(False)
        # menus = self.env["ir.ui.menu"].load_menus(debug=request.session.debug)
        menu = menus.get(menu_id)
        if not menu:
            raise ValueError(f"Menu with ID {menu_id} not found.")
        action = self.env["ir.actions.act_window"].browse(menu["actionID"])
        if not action.exists():
            raise ValueError(f"The action associated with menu ID {menu_id} does not exist.")

        action_dict = action._get_action_dict()
        if action_dict.get("res_model") != model_name:
            raise ValueError(
                f"The model '{model_name}' does not match the model of the action associated with menu ID {menu_id}.")

        available_views = [view[1] for view in action_dict.get("views", [])]
        if "list" not in available_views:
            raise ValueError(f"List view is not available for the action associated with menu ID {menu_id}.")

        bus_data = {
            "menuID": menu_id,
            "selectedFilters": selected_filters,
            "selectedGroupBys": selected_groupbys,
            "search": search,
        }

        if self.env.context.get("ai_session_identifier"):
            bus_data["aiSessionIdentifier"] = self.env.context["ai_session_identifier"]

        if domain := self._parse_domain(model_name, custom_domain):
            bus_data["customDomain"] = domain

        self.env.user._bus_send("AI_EN_OPEN_MENU_LIST", bus_data)

    def _ai_tool_open_menu_kanban(self, menu_id, model_name, selected_filters, selected_groupbys, search,
                                  custom_domain=None):
        validate_search_terms(search)
        validate_groupbys(self.env[model_name], selected_groupbys)

        # menus = self.env["ir.ui.menu"].load_menus(debug=request.session.debug)
        menus = self.env["ir.ui.menu"].load_web_menus(False)
        menu = menus.get(menu_id)
        if not menu:
            raise ValueError(f"Menu with ID {menu_id} not found.")
        action = self.env["ir.actions.act_window"].browse(menu["actionID"])
        if not action.exists():
            raise ValueError(f"The action associated with menu ID {menu_id} does not exist.")

        action_dict = action._get_action_dict()
        if action_dict.get("res_model") != model_name:
            raise ValueError(
                f"The model '{model_name}' does not match the model of the action associated with menu ID {menu_id}.")

        available_views = [view[1] for view in action_dict.get("views", [])]
        if "kanban" not in available_views:
            raise ValueError(f"Kanban view is not available for the action associated with menu ID {menu_id}.")

        bus_data = {
            "menuID": menu_id,
            "selectedFilters": selected_filters,
            "selectedGroupBys": selected_groupbys,
            "search": search,
        }

        if self.env.context.get("ai_session_identifier"):
            bus_data["aiSessionIdentifier"] = self.env.context["ai_session_identifier"]

        if domain := self._parse_domain(model_name, custom_domain):
            bus_data["customDomain"] = domain

        self.env.user._bus_send("AI_EN_OPEN_MENU_KANBAN", bus_data)

    def _ai_tool_open_menu_pivot(self, menu_id, model_name, selected_filters, row_groupbys, col_groupbys, measures,
                                 search, custom_domain=None):
        validate_search_terms(search)
        validate_groupbys(self.env[model_name], row_groupbys)
        validate_groupbys(self.env[model_name], col_groupbys)
        validate_measures(self.env[model_name], measures)

        # menus = self.env["ir.ui.menu"].load_menus(debug=request.session.debug)
        menus = self.env["ir.ui.menu"].load_web_menus(False)
        menu = menus.get(menu_id)
        if not menu:
            raise ValueError(f"Menu with ID {menu_id} not found.")
        action = self.env["ir.actions.act_window"].browse(menu["actionID"])
        if not action.exists():
            raise ValueError(f"The action associated with menu ID {menu_id} does not exist.")

        # Log menu and action details
        menu_obj = self.env["ir.ui.menu"].browse(menu_id)
        _logger.info("Opening pivot view for menu '%s' (ID: %s) with action '%s' (ID: %s)",
                     menu_obj.name, menu_id, action.name, action.id)

        action_dict = action._get_action_dict()
        if action_dict.get("res_model") != model_name:
            raise ValueError(
                f"The model '{model_name}' does not match the model of the action associated with menu ID {menu_id}.")

        # Parse measures and extract ordering information
        parsed_measures = []
        sorted_column = None
        for measure_str in measures:
            measure_parts = measure_str.strip().split()
            measure_name = measure_parts[0]

            if len(measure_parts) > 1:
                order_part = measure_parts[1].lower()
                if order_part in ['asc', 'desc']:
                    order = order_part
                    # Set the first measure with ordering as the sorted column
                    if sorted_column is None:
                        sorted_column = {
                            'measure': measure_name,
                            'order': order
                        }
                else:
                    raise ValueError(
                        f"Invalid ordering specification '{measure_parts[1]}' for measure '{measure_name}'. Use 'asc' or 'desc'.")

            parsed_measures.append(measure_name)

        # Validate measures
        for measure in parsed_measures:
            if measure != "__count" and measure not in self.env[model_name]._fields:
                raise ValueError(f"Measure '{measure}' not found in model '{model_name}' for menu ID {menu_id}.")

        # Check if pivot view is in available views
        available_views = [view[1] for view in action_dict.get("views", [])]
        if "pivot" not in available_views:
            raise ValueError(f"Pivot view is not available for the action associated with menu ID {menu_id}.")

        bus_data = {
            "menuID": menu_id,
            "model": model_name,
            "selectedFilters": selected_filters or [],
            "rowGroupBys": row_groupbys or [],
            "colGroupBys": col_groupbys or [],
            "measures": parsed_measures or [],
            "search": search or [],
        }

        if self.env.context.get("ai_session_identifier"):
            bus_data["aiSessionIdentifier"] = self.env.context["ai_session_identifier"]

        # Add sorting information if available
        if sorted_column:
            bus_data["sortedColumn"] = sorted_column

        if domain := self._parse_domain(model_name, custom_domain):
            bus_data["customDomain"] = domain

        self.env.user._bus_send("AI_EN_OPEN_MENU_PIVOT", bus_data)

    def _ai_tool_open_menu_graph(
            self, menu_id, model_name, selected_filters, selected_groupbys, measure, mode, order, search,
            stacked=False, cumulated=False, custom_domain=None):
        """
        Opens a graph view for the specified menu ID with the given parameters.
        """
        validate_search_terms(search)
        validate_groupbys(self.env[model_name], selected_groupbys)
        validate_measures(self.env[model_name], [measure])

        debug = request.session.debug if request else True
        menus = self.env["ir.ui.menu"].load_menus(debug=debug)
        menu = menus.get(menu_id)
        if not menu:
            raise ValueError(f"Menu with ID {menu_id} not found.")
        action = self.env["ir.actions.act_window"].browse(menu["action_id"])
        if not action.exists():
            raise ValueError(f"The action associated with menu ID {menu_id} does not exist.")

        # Log menu and action details
        menu_obj = self.env["ir.ui.menu"].browse(menu_id)
        _logger.info("Opening graph view for menu '%s' (ID: %s) with action '%s' (ID: %s)",
                     menu_obj.name, menu_id, action.name, action.id)

        action_dict = action._get_action_dict()
        if action_dict.get("res_model") != model_name:
            raise ValueError(
                f"The model '{model_name}' does not match the model of the action associated with menu ID {menu_id}.")

        # Validate measure
        if measure != "__count" and measure not in self.env[model_name]._fields:
            raise ValueError(f"Measure '{measure}' not found in model '{model_name}' for menu ID {menu_id}.")

        # Validate mode
        if mode not in ["bar", "line", "pie"]:
            raise ValueError(f"Invalid mode '{mode}'. Must be 'bar', 'line', or 'pie'.")

        # Validate order
        if order not in ["ASC", "DESC"]:
            raise ValueError(f"Invalid order '{order}'. Must be 'ASC' or 'DESC'.")

        # Check if graph view is in available views
        available_views = [view[1] for view in action_dict.get("views", [])]
        if "graph" not in available_views:
            raise ValueError(f"Graph view is not available for the action associated with menu ID {menu_id}.")

        bus_data = {
            "menuID": menu_id,
            "selectedFilters": selected_filters,
            "groupBys": selected_groupbys or [],
            "measure": measure,
            "mode": mode,
            "order": order,
            "stacked": stacked,
            "cumulated": cumulated,
            "search": search or [],
        }

        if self.env.context.get("ai_session_identifier"):
            bus_data["aiSessionIdentifier"] = self.env.context["ai_session_identifier"]

        if domain := self._parse_domain(model_name, custom_domain):
            bus_data["customDomain"] = domain

        self.env.user._bus_send("AI_EN_OPEN_MENU_GRAPH", bus_data)

    def _ai_tool_compute_report_measures(self, action_id, model):
        if model not in self.env:
            raise ValueError(f"Model '{model}' not found.")

        # action = self.env["ir.actions.act_window"].browse(action_id)
        # if not action.exists():
        #     raise ValueError(f"The action associated with menu ID {action_id} does not exist.")

        try:
            action_id = int(action_id)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid action_id format: {action_id}")

            # 2. 加上 .sudo() 提权！突破 Odoo 权限拦截
        action = self.env["ir.actions.act_window"].sudo().browse(action_id)

        # 3. 智能容错：如果找不到动作，极有可能是 AI 把 menu_id 当成 action_id 传进来了
        if not action.exists():
            # 我们去菜单表里找找，看这个 ID 是不是个菜单
            menu = self.env["ir.ui.menu"].sudo().browse(action_id)
            if menu.exists() and menu.action and menu.action._name == 'ir.actions.act_window':
                action = menu.action  # 纠正 action 对象
            else:
                raise ValueError(f"The action associated with ID {action_id} does not exist.")

        action_dict = action._get_action_dict()
        if action_dict.get("res_model") != model:
            raise ValueError(
                f"The model '{model}' does not match the model of the action associated with menu ID {action_id}.")

        # Get field definitions
        model_obj = self.env[model]
        fields = model_obj.fields_get()

        # Get view information to determine field attributes
        views = model_obj.get_views(
            [*action_dict["views"]],
            options={
                "action_id": action.id,
                "toolbar": False,
            },
        )["views"]

        # Extract field attributes from pivot view if available
        field_attrs = {}
        pivot_view = views.get("pivot")
        if pivot_view and pivot_view.get("arch"):
            view_tree = etree.fromstring(pivot_view["arch"], None)
            for field_element in view_tree.xpath(".//field"):
                field_name = field_element.get("name")
                if field_name:
                    field_attrs[field_name] = {
                        "isInvisible": field_element.get("invisible") == "1",
                        "string": field_element.get("string"),
                    }

        # Compute measures using our Python implementation
        measures = compute_report_measures(fields, field_attrs)

        # Convert measures to CSV format with pipe delimiter
        csv_result = "field_name|field_display_name|field_type|aggregator|sortable\n"

        for field_name, field_info in measures.items():
            field_display_name = field_info.get("string", "")
            field_type = field_info.get("type", "")
            aggregator = field_info.get("aggregator", "")
            sortable = str(field_info.get("sortable", "")).lower()

            csv_result += f"{field_name}|{field_display_name}|{field_type}|{aggregator}|{sortable}\n"

        return csv_result.strip()

    def _ai_tool_search(self, model_name, domain="", fields: list[str] | None = None, offset: int = 0,
                        limit: int | None = None, order: str | None = None):
        try:
            parsed_domain = json.loads(domain)
            search_result = self.env[model_name].search_read(parsed_domain, fields, offset, limit, order)
            return search_result
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format for custom domain: {e}")

    def _ai_tool_read_group(self, model_name, domain, groupby: list[str] = [], aggregates: list[str] = [],
                            having: str = "[]", offset: int = 0, limit: int | None = None, order: str | None = None):
        try:
            parsed_domain = json.loads(domain)
            parsed_having = ""
            if having:
                parsed_having = json.loads(having)
            result = self.env[model_name]._read_group(parsed_domain, groupby, aggregates, parsed_having, offset, limit,
                                                      order)
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format for custom domain: {e}")

    def _ai_tool_adjust_search(self, model_name, remove_facets=None, toggle_filters=None, toggle_groupbys=None,
                               apply_searches=None, measures=None, mode=None, order=None, stacked=None, cumulated=None,
                               custom_domain=None, switch_view_type=None):
        validate_search_terms(apply_searches)
        validate_groupbys(self.env[model_name], toggle_groupbys)

        payload = {
            "removeFacets": remove_facets or [],
            "toggleFilters": toggle_filters or [],
            "toggleGroupBys": toggle_groupbys or [],
            "applySearches": apply_searches or [],
            "measures": measures or [],
            "mode": mode or None,
            "order": order or "ASC",
            "stacked": stacked or False,
            "cumulated": cumulated or False,
            "switchViewType": switch_view_type or False
        }

        available_view_types = self.env.context.get("current_view_info", {}).get("available_view_types", [])
        if switch_view_type and switch_view_type not in available_view_types:
            raise ValueError(
                f"Requested view type '{switch_view_type}' is not in the available_view_types: {available_view_types}")

        if self.env.context.get("ai_session_identifier"):
            payload["aiSessionIdentifier"] = self.env.context["ai_session_identifier"]

        if domain := self._parse_domain(model_name, custom_domain):
            payload["customDomain"] = domain

        self.env.user._bus_send("AI_EN_ADJUST_SEARCH", payload)
