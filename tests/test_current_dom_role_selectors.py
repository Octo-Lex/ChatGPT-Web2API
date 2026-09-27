import inspect
from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.completion_detector import CompletionDetector

def test_current_chatgpt_role_selectors_are_supported():
    src = inspect.getsource(CDPDriver) + inspect.getsource(CompletionDetector)
    assert "data-content-search-unit-key$=\\\":assistant\\\"" in src
    assert "data-content-search-unit-key$=\\\":user\\\"" in src
    assert "data-message-author-role" in src
