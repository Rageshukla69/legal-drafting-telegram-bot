import re
import logging
from dataclasses import dataclass
from typing import Dict, Any, List

@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str]

class DavaValidator:
    """
    The final deterministic gatekeeper before document rendering.
    Intercepts hallucinated facts, structural violations, and formatting leaks.
    """
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # Heuristics for catching reliefs leaked into factual paragraphs
        self.relief_keywords = [
            r"प्रार्थना करता है", 
            r"राहत चाहता है", 
            r"रोका जाए", 
            r"मना किया जाए", 
            r"निषेधाज्ञा जारी की जाए"
        ]
        
        # Heuristics for catching hallucinated statutes/sections
        self.statute_patterns = [
            r"धारा \d+", 
            r"Section \d+", 
            r"सीपीसी", 
            r"C\.?P\.?C\.?",
            r"अधिनियम"
        ]

    def validate(self, draft_schema: Dict[str, Any], case_data: Dict[str, Any]) -> ValidationResult:
        self.logger.info(f"Validating draft for case: {case_data.get('case_id', 'unknown')}")
        errors = []

        # 1. Structural Completeness Check
        required_keys = ['court_name', 'plaintiffs', 'defendants', 'factual_averments', 'prayer']
        for key in required_keys:
            if not draft_schema.get(key):
                errors.append(f"संरचनात्मक त्रुटि: '{key}' अनुभाग गायब या रिक्त है।")

        factual_averments = draft_schema.get('factual_averments', [])
        
        # 2. Formatting Leak Check (Numbering & "यह कि")
        for i, averment in enumerate(factual_averments):
            if re.match(r"^(\d+|\([कखगabc]\)|[ivx]+)[\.\)]?\s*", averment):
                errors.append(f"प्रारूपण त्रुटि: तथ्य {i+1} में AI द्वारा संख्या (numbering) जोड़ी गई है।")
            if averment.strip().startswith("यह कि"):
                errors.append(f"प्रारूपण त्रुटि: तथ्य {i+1} 'यह कि' से शुरू हो रहा है, जिसे रेंडरर द्वारा जोड़ा जाना चाहिए।")

        # 3. Relief as Fact Check
        for i, averment in enumerate(factual_averments):
            for keyword in self.relief_keywords:
                if re.search(keyword, averment):
                    errors.append(f"तथ्यात्मक त्रुटि: तथ्य {i+1} में राहत/प्रार्थना ('{keyword}') शामिल है। इसे केवल Prayer अनुभाग में होना चाहिए।")
                    break

        # 4. Factual Escalation Check (The Golden Rule)
        # Prevents AI from turning "attempt/threat" into a "completed act".
        raw_facts = " ".join(case_data.get('facts', []))
        draft_facts = " ".join(factual_averments)
        
        has_threat = "धमकी" in raw_facts or "प्रयास" in raw_facts
        has_dispossession = "कब्जा कर लिया" in raw_facts or "बेदखल" in raw_facts
        
        if has_threat and not has_dispossession:
            if "कब्जा कर लिया" in draft_facts or "बेदखल कर दिया" in draft_facts:
                errors.append("तथ्यात्मक त्रुटि: AI ने 'धमकी/प्रयास' को 'पूर्ण कब्जे/बेदखली' में बदल दिया है।")

        # 5. Hallucinated Statutes Check
        # If the user didn't mention specific acts/sections, the AI shouldn't invent them in the facts.
        for pattern in self.statute_patterns:
            if re.search(pattern, draft_facts, re.IGNORECASE) and not re.search(pattern, raw_facts, re.IGNORECASE):
                errors.append(f"कानूनी त्रुटि: AI ने तथ्य अनुभाग में एक काल्पनिक क़ानून या धारा ('{pattern}') जोड़ दी है।")
                break
                
        # 6. Duplication Check (Cause of Action vs Facts)
        coa = draft_schema.get('cause_of_action', "")
        if coa and coa in factual_averments:
            errors.append("संरचनात्मक त्रुटि: 'वाद कारण' (Cause of Action) को तथ्यात्मक पैराग्राफ में हूबहू दोहराया गया है।")

        is_valid = len(errors) == 0
        if not is_valid:
            self.logger.warning(f"Validation failed with {len(errors)} errors: {errors}")
            
        return ValidationResult(is_valid=is_valid, errors=errors)
