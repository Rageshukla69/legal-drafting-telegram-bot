import json
import logging
from typing import Dict, Any

class DavaComposer:
    """
    Translates the structural plan into formal, advocate-level Hindi legal text.
    Enforces strict factual fidelity, ensuring AI does not invent acts, ownership, or legal provisions.
    """
    def __init__(self, azure_client, retriever, schema_path="app/drafting_engine/draft_schema.json"):
        self.azure_client = azure_client
        self.retriever = retriever
        self.logger = logging.getLogger(__name__)
        
        with open(schema_path, "r", encoding="utf-8") as f:
            self.response_schema = json.load(f)

    def draft_sections(self, case_data: Dict[str, Any], structural_plan: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"Composing final draft for case: {case_data.get('case_id', 'unknown')}")

        # Retrieve style examples from the historical corpus based on the suit type
        nature_of_suit = structural_plan.get('nature_of_suit', 'Dava')
        style_examples = self.retriever.retrieve_relevant_content(nature_of_suit)

        system_prompt = self._build_system_prompt(style_examples)
        
        user_payload = {
            "parties_and_court": {
                "court": case_data.get("court_name", "[अज्ञात न्यायालय]"),
                "plaintiffs": case_data.get("plaintiffs", ""),
                "defendants": case_data.get("defendants", "")
            },
            "structural_plan": structural_plan
        }
        
        user_prompt = f"Draft the final legal Hindi text for this case:\n{json.dumps(user_payload, ensure_ascii=False, indent=2)}"

        try:
            response = self.azure_client.generate_structured_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=self.response_schema
            )
            draft = json.loads(response)
            return self._clean_draft(draft)
        except Exception as e:
            self.logger.error(f"Drafting composer failed: {str(e)}")
            return self._fallback_draft(user_payload)

    def _build_system_prompt(self, style_examples: str) -> str:
        return f"""You are an expert Indian Civil Court Advocate drafting a formal Dava in clean Unicode Hindi.
Your task is to take a 'Structural Plan' and draft the final professional text.

CRITICAL FACTUAL FIDELITY RULES:
1. NEVER change material facts. Do not invent dates, names, or locations.
2. THREAT VS ACT: If the facts state the defendant 'threatened to dispossess' or 'attempted', YOU MUST NOT write 'कब्जा कर लिया' (dispossessed). Write 'कब्जा करने का प्रयास/धमकी दी'.
3. OWNERSHIP VS POSSESSION: If the facts state the plaintiff is 'cultivating' (खेती कर रहा है), DO NOT invent that they are the absolute owner ('पूर्ण स्वामी') unless explicitly stated.
4. RELIEF IS NOT A FACT: Never put prayer requests (e.g., 'वादी चाहता है कि...') in the factual_averments array. Reliefs belong ONLY in the prayer array.

DRAFTING & FORMATTING RULES:
1. NO PARAGRAPH NUMBERS: Do not include "1.", "2." etc. in the arrays. The rendering engine will automatically number them.
2. NO 'यह कि': Do not start sentences with 'यह कि'. The rendering engine will prepend this automatically.
3. NO HEADINGS: Do not generate AI-style headings like "वादी का परिचय" or "वाद के तथ्य". Output only the substantive paragraph text.
4. PARTY BLOCK: Format plaintiffs and defendants cleanly without extra conversational text.

STYLE REFERENCE (Use ONLY for vocabulary and tone, DO NOT copy facts/names from here):
{style_examples}
"""

    def _clean_draft(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """
        Final safety sweep to ensure no numbering or 'यह कि' leaked into the final arrays.
        """
        clean_averments = []
        for averment in draft.get('factual_averments', []):
            # Strip accidental prefixes left by AI despite instructions
            cleaned = averment.lstrip("0123456789.()[] ").strip()
            if cleaned.startswith("यह कि "):
                cleaned = cleaned[6:].strip()
            if cleaned:
                clean_averments.append(cleaned)
                
        draft['factual_averments'] = clean_averments
        return draft

    def _fallback_draft(self, user_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Provides a safe passthrough if AI composition fails."""
        self.logger.warning("Using fallback composer draft.")
        plan = user_payload.get("structural_plan", {})
        return {
            "court_name": user_payload["parties_and_court"].get("court", ""),
            "plaintiffs": user_payload["parties_and_court"].get("plaintiffs", ""),
            "defendants": user_payload["parties_and_court"].get("defendants", ""),
            "nature_of_suit": plan.get("nature_of_suit", "वाद पत्र"),
            "factual_averments": plan.get("factual_averments", []),
            "cause_of_action": plan.get("cause_of_action", ""),
            "jurisdiction": plan.get("jurisdiction", ""),
            "valuation_and_court_fee": plan.get("valuation_and_court_fee", ""),
            "prayer": plan.get("prayer", [])
        }
