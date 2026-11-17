from app.main import app, redact

def test_health_route_exists():
    assert any(getattr(r, 'path', None) == '/api/health' for r in app.routes)

def test_redacts_obvious_username_and_phone():
    text = 'Contact @someusername or +1 212 555 0199'
    out = redact(text)
    assert '@someusername' not in out
    assert '+1 212 555 0199' not in out

def test_redact_keeps_ordinary_text():
    assert redact('Hello, how are you?') == 'Hello, how are you?'
