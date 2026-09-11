import json
import logging
from typing import Dict, Any

class DavaStructurePlanner:
    """
    Acts as the legal architect. Determines the logical flow and section 
    requirements of the pleading without writing the final polished Hindi.
    Prevents AI from generating conversational summaries or generic headings.
    """
    def __init__(self, azure_client, schema_path="app/drafting_engine/structure_schema.json"):
        self.azure_client = azure_client
        self.logger = logging.getLogger(__name__)
        
        with open(schema_path, "r", encoding="utf-8") as f:
            self.response_schema = json.load(f)

    def generate_plan(self, case_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Maps collected conversational facts into a strict legal structure.
        """
        self.logger.info(f"Generating structural plan for case ID: {case_data.get('case_id', 'unknown')}")

        system_prompt = (
            "You are a structural planner for an Indian Advocate (Civil Law). "
            "Your job is to organize the provided raw case facts into a rigid JSON structure.\n\n"
            "CRITICAL RULES:\n"
            "1. DO NOT INVENT FACTS: If the plaintiff says 'threatened to dispossess', DO NOT write 'dispossessed'.\n"
            "2. NO RELIEFS AS FACTS: If the user says 'I want to stop him', put this ONLY in the 'prayer' array, NEVER in 'factual_averments'.\n"
            "3. NO PARAGRAPH NUMBERS: Do not include '1.', '2.', 'यह कि' etc. The renderer handles this.\n"
            "4. NO HEADINGS: Do not output generic headings like 'वाद के तथ्य' or 'वादी का परिचय'.\n"
            "5. NO DUPLICATION: Do not repeat the same event in multiple arrays unless legally required (e.g., date of threat in facts AND cause of action)."
        )

        user_prompt = f"Organize the following collected case data into the required legal JSON structure:\n{json.dumps(case_data, ensure_ascii=False, indent=2)}"

        try:
            # Enforce JSON output matching the schema to guarantee deterministic dictionary keys
            response = self.azure_client.generate_structured_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=self.response_schema
            )
            
            plan = json.loads(response)
            return self._sanitize_plan(plan)
            
        except Exception as e:
            self.logger.error(f"Structural planning failed: {str(e)}")
            return self._get_fallback_plan(case_data)

    def _sanitize_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Secondary defense against AI hallucinations before passing to the drafting composer.
        """
        # Strip any accidental numbering the AI might have snuck into the strings
        sanitized_averments = []
        for fact in plan.get('factual_averments', []):
            clean_fact = fact.lstrip("0123456789.()[] ").replace("यह कि ", "").strip()
            sanitized_averments.append(clean_fact)
        
        plan['factual_averments'] = sanitized_averments
        return plan

    def _get_fallback_plan(self, case_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Safe fallback ensuring the bot never crashes if Azure returns malformed JSON.
        """
        self.logger.warning("Using fallback structural plan.")
        return {
            "document_type": "Dava",
            "nature_of_suit": case_data.get('nature_of_suit', 'वाद पत्र'),
            "factual_averments": case_data.get('facts', []),
            "cause_of_action": case_data.get('cause_of_action', '[वाद कारण स्पष्ट नहीं]'),
            "jurisdiction": case_data.get('jurisdiction_facts', '[अधिकारिता स्पष्ट नहीं]'),
            "valuation_and_court_fee": case_data.get('valuation', ''),
            "prayer": case_data.get('reliefs', [])
        }
