"""Conservative Hindi question generator.

Questions are based only on fields absent from the structured case state.
"""
from .conversation_state import next_questions

TEMPLATES = {
    "court_name": "1. किस न्यायालय में वाद दाखिल होना है?",
    "plaintiffs": "2. वादी का/वादियों का पूरा नाम क्या है?",
    "defendants": "3. प्रतिवादी का/प्रतिवादियों का पूरा नाम क्या है?",
    "facts": "4. घटना/विवाद के मुख्य तथ्य क्रम से बताइए—क्या हुआ, कब हुआ और किसके बीच हुआ?",
    "reliefs": "5. वादी न्यायालय से कौन-कौन सी राहत चाहता है?",
    "plaintiff_intro": "6. वादी का पिता/पति का नाम, आयु और पूरा पता क्या है?",
    "cause_of_action": "7. वाद-कारण से संबंधित स्पष्ट तथ्य क्या हैं और वह कब उत्पन्न हुआ?",
    "jurisdiction_facts": "8. इस न्यायालय के क्षेत्राधिकार से संबंधित उपलब्ध स्पष्ट तथ्य क्या हैं?",
    "valuation": "9. वाद का मूल्यांकन कितना है, यदि पहले से निर्धारित/उपलब्ध है?",
    "court_fee": "10. न्यायालय शुल्क से संबंधित उपलब्ध जानकारी क्या है?",
}

def questions_for_state(facts, max_questions=4):
    return [TEMPLATES[k] for k,_ in next_questions(facts, max_questions)]
