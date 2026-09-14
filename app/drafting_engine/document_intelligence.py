"""Azure AI Document Intelligence v4 REST integration.

Uses the S0 resource and the GA 2024-11-30 API directly, so the bot does not
need the Document Intelligence Python SDK. The model is fixed to prebuilt-layout
because it provides OCR plus document structure for legal documents.
"""
from __future__ import annotations
import base64, os, time
from dataclasses import dataclass
from typing import Any
import requests

class DocumentIntelligenceError(RuntimeError):
    pass

@dataclass(frozen=True)
class OCRResult:
    text: str
    page_count: int
    model_id: str


def _endpoint() -> str:
    value=os.getenv('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT','').strip().rstrip('/')
    if not value: raise DocumentIntelligenceError('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set.')
    return value

def _key() -> str:
    value=os.getenv('AZURE_DOCUMENT_INTELLIGENCE_KEY','').strip()
    if not value: raise DocumentIntelligenceError('AZURE_DOCUMENT_INTELLIGENCE_KEY is not set.')
    return value

def _json_error(resp: requests.Response) -> str:
    try:
        body=resp.json(); err=body.get('error') or body
        return str(err.get('message') or err)[:1200] if isinstance(err,dict) else str(err)[:1200]
    except Exception:
        return resp.text[:1200]

def analyze_document_bytes(data: bytes, content_type: str='application/octet-stream', filename: str='document') -> OCRResult:
    if not data: raise DocumentIntelligenceError('The uploaded document is empty.')
    model=os.getenv('AZURE_DOCUMENT_INTELLIGENCE_MODEL','prebuilt-layout').strip() or 'prebuilt-layout'
    if model != 'prebuilt-layout': raise DocumentIntelligenceError('AZURE_DOCUMENT_INTELLIGENCE_MODEL must be prebuilt-layout.')
    api=os.getenv('AZURE_DOCUMENT_INTELLIGENCE_API_VERSION','2024-11-30').strip() or '2024-11-30'
    timeout=float(os.getenv('AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS','600'))
    poll=float(os.getenv('AZURE_DOCUMENT_INTELLIGENCE_POLL_SECONDS','1.5'))
    url=f"{_endpoint()}/documentintelligence/documentModels/{model}:analyze"
    params={'api-version':api,'outputContentFormat':'markdown'}
    headers={'Ocp-Apim-Subscription-Key':_key(),'Content-Type':content_type or 'application/octet-stream'}
    # REST v4 accepts a base64Source request. This avoids SDK-version drift and works for binary PDFs/images.
    payload={'base64Source':base64.b64encode(data).decode('ascii')}
    try:
        r=requests.post(url,params=params,headers={**headers,'Content-Type':'application/json'},json=payload,timeout=90)
    except requests.RequestException as exc:
        raise DocumentIntelligenceError(f'Azure Document Intelligence network error: {exc}') from exc
    if r.status_code not in (200,202):
        raise DocumentIntelligenceError(f'Azure Document Intelligence rejected {filename}: {_json_error(r)}')
    if r.status_code==200:
        result=r.json()
    else:
        operation=r.headers.get('Operation-Location') or r.headers.get('operation-location')
        if not operation: raise DocumentIntelligenceError('Azure returned 202 without an Operation-Location.')
        deadline=time.monotonic()+timeout
        while True:
            if time.monotonic()>deadline: raise DocumentIntelligenceError('Azure Document Intelligence timed out while analyzing the document.')
            try: rr=requests.get(operation,headers={'Ocp-Apim-Subscription-Key':_key()},timeout=60)
            except requests.RequestException as exc: raise DocumentIntelligenceError(f'Azure polling network error: {exc}') from exc
            if rr.status_code!=200:
                raise DocumentIntelligenceError(f'Azure polling failed: {_json_error(rr)}')
            result=rr.json(); status=str(result.get('status','')).lower()
            if status=='succeeded': break
            if status in {'failed','cancelled'}: raise DocumentIntelligenceError(f'Azure analysis status: {status}. { _json_error(rr) }')
            time.sleep(poll)
    analyze=result.get('analyzeResult') or result.get('result') or result
    text=str(analyze.get('content') or '').strip()
    if not text:
        chunks=[]
        for page in analyze.get('pages') or []:
            for line in page.get('lines') or []:
                c=line.get('content')
                if c: chunks.append(str(c))
        text='\n'.join(chunks).strip()
    pages=analyze.get('pages') or []
    return OCRResult(text=text,page_count=len(pages) or 1,model_id=model)
