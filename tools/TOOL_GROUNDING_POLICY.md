# Tool Grounding Policy

Parameters listed here are structural/runtime fields. They are not required to appear in the user message for grounding checks.

## Skip Parameters

- response_format
- trace_enabled
- config_path
- output_style
- read_mode
- limit
- max_results
- max_chars
- follow_redirects
- alternatives
- image_base64
- mime_type
- timeout_ms

## Loose Grounding Tools

Tools that may run when the tool name is grounded in the user turn even if a specific argument string does not match literally:

- memory_recall
- memory_assess
- system_control
- camera_vision
