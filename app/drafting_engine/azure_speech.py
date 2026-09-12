from __future__ import annotations
import json, os
import requests

class AzureSpeechError(RuntimeError): pass

def transcribe_voice(audio_bytes:bytes, filename='telegram_voice.ogg')->str:
    key=os.getenv('AZURE_SPEECH_KEY','').strip(); endpoint=os.getenv('AZURE_SPEECH_ENDPOINT','').strip().rstrip('/')
    if not key or not endpoint: raise AzureSpeechError('AZURE_SPEECH_KEY and AZURE_SPEECH_ENDPOINT are required.')
    definition={'enhancedMode':{'enabled':True,'model':os.getenv('AZURE_SPEECH_MODEL','MAI-Transcribe-2')},'phraseList':{'phrases':[
        'गाटा','खसरा','खतौनी','दाखिल खारिज','बैनामा','वादपत्र','वादी','प्रतिवादी','न्यायालय','तहसील','लेखपाल','नायब तहसीलदार','स्थगनादेश','निषेधाज्ञा','अधिकार','कब्जा','सीमांकन','पैमाइश','रजिस्ट्री','दाखिल-खारिज','प्रार्थनापत्र','शपथपत्र','साक्ष्य','प्रतिज्ञापत्र','मुकदमा','वाद','अस्थायी निषेधाज्ञा','स्थायी निषेधाज्ञा','सिविल जज','बिधूना','औरैया','एडवोकेट','वी०डी० शुक्ला'
    ],'biasingWeight':1.6}}
    locale=os.getenv('AZURE_SPEECH_LOCALES','').strip()
    if locale: definition['locales']=[locale]
    mime='audio/ogg'
    if filename.lower().endswith('.wav'): mime='audio/wav'
    elif filename.lower().endswith('.mp3'): mime='audio/mpeg'
    elif filename.lower().endswith('.flac'): mime='audio/flac'
    url=f'{endpoint}/speechtotext/transcriptions:transcribe?api-version=2025-10-15'
    files={'audio':(filename,audio_bytes,mime),'definition':(None,json.dumps(definition,ensure_ascii=False),'application/json')}
    try: r=requests.post(url,headers={'Ocp-Apim-Subscription-Key':key},files=files,timeout=float(os.getenv('AZURE_SPEECH_TIMEOUT_SECONDS','90')))
    except requests.RequestException as e: raise AzureSpeechError(f'Azure Speech network error: {e}') from e
    if not r.ok: raise AzureSpeechError(f'Azure Speech HTTP {r.status_code}: {r.text[:3000]}')
    try: body=r.json()
    except ValueError as e: raise AzureSpeechError('Azure Speech returned invalid JSON.') from e
    for key_name in ('combinedPhrases','phrases'):
        vals=body.get(key_name) or []
        text=' '.join(str(x.get('text','')).strip() for x in vals if isinstance(x,dict) and str(x.get('text','')).strip()).strip()
        if text: return text
    raise AzureSpeechError(f'Azure Speech returned no transcript: {body}')
