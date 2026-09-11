import logging
from typing import Dict, Any, Tuple

class DavaOrchestrator:
    """
    Coordinates the legal drafting pipeline, ensuring AI outputs are bound 
    by strict structural and factual constraints before document rendering.
    """
    def __init__(self, state_store, intake_engine, planner, composer, validator, renderer):
        self.state_store = state_store
        self.intake_engine = intake_engine
        self.planner = planner
        self.composer = composer
        self.validator = validator
        self.renderer = renderer
        self.logger = logging.getLogger(__name__)

    def extract_and_collect(self, user_id: str, user_text: str) -> str:
        """
        Restored method for bot.py compatibility.
        Updates the active case state with new conversational facts without triggering a full draft.
        """
        self.logger.info(f"Extracting facts for active case state: {user_id}")
        
        # 1. Fetch current conversational state
        current_state = self.state_store.get_state(user_id) or {}
        
        # 2. Extract structured entities (Parties, Events, Property) safely
        updated_state, bot_response = self.intake_engine.process_input(current_state, user_text)
        
        # 3. Persist state safely to prevent data loss
        self.state_store.save_state(user_id, updated_state)
        
        return bot_response

    def execute_drafting_pipeline(self, user_id: str) -> str:
        """
        Executes the multi-stage AI drafting pipeline with built-in validation.
        """
        case_data = self.state_store.get_state(user_id)
        if not case_data:
            return "त्रुटि: कोई सक्रिय मामला नहीं मिला। कृपया /newcase से प्रारंभ करें।"

        try:
            # Stage 1: Structural Planner dictates the exact sections required
            structural_plan = self.planner.generate_plan(case_data)
            
            # Stage 2: Composer drafts ONLY within the planner's rigid layout
            draft_content = self.composer.draft_sections(case_data, structural_plan)
            
            # Stage 3: Validator intercepts hallucinated statutes or facts
            validation_result = self.validator.validate(draft_content, case_data)
            
            if not validation_result.is_valid:
                self.logger.warning(f"Validation failed: {validation_result.errors}. Retrying...")
                # Controlled retry logic goes here (Placeholder for Phase 4)
                return f"सत्यापन विफल: {validation_result.errors[0]}"

            # Stage 4: Deterministic generation
            docx_path = self.renderer.render_docx(draft_content, f"dava_{user_id}.docx")
            return docx_path
            
        except Exception as e:
            self.logger.error(f"Pipeline failure for {user_id}: {str(e)}")
            return "दस्तावेज़ तैयार करने में तकनीकी समस्या आई। कृपया बाद में पुनः प्रयास करें।"
