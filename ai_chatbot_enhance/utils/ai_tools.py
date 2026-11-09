from lxml import html
from odoo.tools.mail import html_to_inner_content


def parse_ai_prompt_values(env, prompt, comodel, replace_prompt=True):
    tree = html.fromstring(prompt)

    prompt_fields = set()
    for prompt_field_element in tree.xpath('//span[@data-ai-field]'):
        field_path = prompt_field_element.attrib.get('data-ai-field')
        if replace_prompt:
            if field_path:
                prompt_field_element.text = f"{{{{{field_path}}}}}"
            else:
                prompt_field_element.drop_tree()
        prompt_fields.add(field_path)

    inserted_record_ids = set()
    formatted_allowed_records = {}
    if comodel:
        inserted_record_elements = tree.xpath('//span[@data-ai-record-id]')
        inserted_record_ids = {
            int(record_id)
            for inserted_record_element
            in inserted_record_elements
            if (record_id := inserted_record_element.attrib.get('data-ai-record-id'))
        }
        if replace_prompt:
            allowed_records_by_id = env[comodel].browse(inserted_record_ids).exists().grouped("id")
            for inserted_record_element in inserted_record_elements:
                if allowed_record := allowed_records_by_id.get(
                        int(inserted_record_element.attrib.get('data-ai-record-id'))):
                    record_name_field = (
                        allowed_record._ai_rec_name
                        if hasattr(allowed_record, '_ai_rec_name')
                        else 'display_name'
                    )
                    inserted_record_element.text = allowed_record._ai_truncate(
                        allowed_record[record_name_field])
                else:
                    inserted_record_element.drop_tree()
            formatted_allowed_records = env[comodel].browse(allowed_records_by_id.keys())._ai_format_records()

    if replace_prompt:
        return html_to_inner_content(html.tostring(tree, encoding='unicode')), prompt_fields, formatted_allowed_records
    return prompt, prompt_fields, inserted_record_ids
