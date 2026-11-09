# Part of AI Code Graph.
from odoo import fields, models

EDGE_TYPES = [
    # Python layer
    ('module_depends', "Module depends on"),
    ('model_inherit', "Model inherits (new model)"),
    ('model_extend', "Module extends model in-place"),
    ('model_delegate', "Model delegates to (_inherits)"),
    ('field_of', "Field of model"),
    ('field_comodel', "Field targets model"),
    ('compute_depends', "Compute depends on field"),
    ('method_of', "Method of model"),
    ('method_override', "Module overrides method"),
    ('method_calls', "Method calls method"),
    # XML layer
    ('view_of_model', "View of model"),
    ('view_inherits', "View inherits view"),
    ('view_uses_field', "View displays field"),
    ('view_calls_method', "View button calls method"),
    ('action_targets_model', "Action opens model"),
    ('menu_opens_action', "Menu triggers action"),
    ('template_inherits', "Template inherits template"),
    # JS layer
    ('js_patches', "JS patches component"),
    ('js_registers', "JS registers in registry"),
    ('component_uses_template', "Component renders template"),
    ('js_component_extends', "Component extends component"),
]


class AiCodeEdge(models.Model):
    _name = 'ai.code.edge'
    _description = "Code Graph Edge"
    _order = 'edge_type, id'

    source_id = fields.Many2one(
        'ai.code.node', required=True, ondelete='cascade', index=True)
    target_id = fields.Many2one(
        'ai.code.node', required=True, ondelete='cascade', index=True)
    edge_type = fields.Selection(EDGE_TYPES, required=True, index=True)
    module_name = fields.Char(
        index=True, help="Module that declares this relationship.")
    meta = fields.Json()

    def _edge_payload(self):
        return [{
            'id': e.id,
            'source': e.source_id.id,
            'target': e.target_id.id,
            'type': e.edge_type,
        } for e in self]

    def _compute_display_name(self):
        for e in self:
            e.display_name = (f"{e.source_id.technical_key} -[{e.edge_type}]-> "
                              f"{e.target_id.technical_key}")
