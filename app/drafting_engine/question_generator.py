"""Human-friendly Hindi questions for missing Dava intake fields."""
from .conversation_state import next_questions

TEMPLATES = {
    "court_name": "किस न्यायालय में वाद दाखिल होना है?",
    "plaintiffs": "वादी का/वादियों का पूरा नाम क्या है?",
    "defendants": "प्रतिवादी का/प्रतिवादियों का पूरा नाम क्या है?",
    "facts": "घटना/विवाद के मुख्य तथ्य क्रम से बताइए—क्या हुआ, कब हुआ और किसके बीच हुआ?",
    "reliefs": "वादी न्यायालय से कौन-कौन सी राहत चाहता है?",
    "plaintiff_intro": "वादी का पिता/पति का नाम, आयु और पूरा पता क्या है?",
    "defendant_intro": "प्रतिवादी का पिता/पति का नाम, आयु और पूरा पता क्या है?",
    "property_description": "विवादित संपत्ति का पूरा विवरण क्या है—गाटा/खसरा/प्लॉट संख्या, क्षेत्रफल, सीमाएं आदि जो उपलब्ध हों?",
    "cause_of_action": "वाद-कारण कब और किन घटनाओं से उत्पन्न हुआ?",
    "jurisdiction_facts": "इस न्यायालय के क्षेत्राधिकार से संबंधित उपलब्ध तथ्य क्या हैं?",
    "limitation_facts": "समय-सीमा से संबंधित कोई उपलब्ध तथ्य/तारीख है?",
    "valuation": "वाद का मूल्यांकन कितना है, यदि पहले से निर्धारित है?",
    "court_fee": "न्यायालय शुल्क के बारे में उपलब्ध जानकारी क्या है?",
}

def questions_for_state(facts, max_questions=5):
    return [f"{i}. {TEMPLATES.get(k, label)}" for i, (k, label) in enumerate(next_questions(facts, max_questions), 1)]
