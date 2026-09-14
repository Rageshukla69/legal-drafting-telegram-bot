import base64
from app.drafting_engine import document_intelligence as di

def test_request_shape_and_model(monkeypatch):
    calls={}
    class Resp:
        status_code=202
        headers={'Operation-Location':'https://example.invalid/op'}
        text=''
        def json(self): return {}
    def fake_post(url, **kwargs):
        calls.update(url=url,kwargs=kwargs); return Resp()
    def fake_get(url, **kwargs):
        class R:
            status_code=200
            text=''
            def json(self): return {'status':'succeeded','analyzeResult':{'content':'hello','pages':[{}]}}
        return R()
    monkeypatch.setattr(di.requests,'post',fake_post)
    monkeypatch.setattr(di.requests,'get',fake_get)
    monkeypatch.setenv('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT','https://x.cognitiveservices.azure.com')
    monkeypatch.setenv('AZURE_DOCUMENT_INTELLIGENCE_KEY','secret')
    monkeypatch.setenv('AZURE_DOCUMENT_INTELLIGENCE_POLL_SECONDS','0')
    out=di.analyze_document_bytes(b'abc','application/pdf','x.pdf')
    assert out.model_id=='prebuilt-layout'
    assert out.text=='hello'
    assert 'prebuilt-layout:analyze' in calls['url']
    assert calls['kwargs']['json']['base64Source']==base64.b64encode(b'abc').decode()
