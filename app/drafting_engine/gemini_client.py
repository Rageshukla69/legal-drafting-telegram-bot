from __future__ import annotations
import json, os
from typing import Any
import requests

class GeminiError(RuntimeError): pass

def _schema(s: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(s, dict): return s
    out={}
    m={'object':'OBJECT','array':'ARRAY','string':'STRING','integer':'INTEGER','number':'NUMBER','boolean':'BOOLEAN'}
    if s.get('type'): out['type']=m.get(str(s['type']).lower(),str(s['type']).upper())
    if 'properties' in s: out['properties']={k:_schema(v) for k,v in s['properties'].items()}
    if 'items' in s: out['items']=_schema(s['items'])
    for k in ('required','enum','description','minItems','maxItems','minimum','maximum'):
        if k in s: out[k]=s[k]
    return out

class GeminiClient:
    def __init__(self):
        self.key=os.getenv('GEMINI_API_KEY','').strip()
        self.model=os.getenv('GEMINI_MODEL','gemini-3.8-flash').strip()
        self.timeout=float(os.getenv('GEMINI_TIMEOUT_SECONDS','90'))
        if not self.key: raise GeminiError('GEMINI_API_KEY is not set.')
    def generate_json(self, *, system:str, prompt:str, schema:dict[str,Any], thinking_level='medium') -> dict[str,Any]:
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent'
        gen={'temperature':0,'responseMimeType':'application/json','responseSchema':_schema(schema)}
        level=str(thinking_level or 'medium').lower()
        if level not in {'low','medium','high'}: level='medium'
        gen['thinkingConfig']={'thinkingLevel':level}
        payload={'systemInstruction':{'parts':[{'text':system}]},'contents':[{'role':'user','parts':[{'text':prompt}]}],'generationConfig':gen}
        try:
            r=requests.post(url,params={'key':self.key},json=payload,timeout=self.timeout)
        except requests.RequestException as e: raise GeminiError(f'Gemini network error: {e}') from e
        if not r.ok: raise GeminiError(f'Gemini HTTP {r.status_code}: {r.text[:3000]}')
        try:
            body=r.json(); text=body['candidates'][0]['content']['parts'][0]['text']; result=json.loads(text)
        except (KeyError,IndexError,TypeError,ValueError) as e:
            raise GeminiError(f'Gemini returned invalid structured JSON: {r.text[:2000]}') from e
        if not isinstance(result,dict): raise GeminiError('Gemini response was not a JSON object.')
        return result
