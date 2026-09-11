import os
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

class LegalDocumentRenderer:
    """
    Deterministic document generator. Strips all formatting decisions away from the AI.
    """
    def __init__(self, assets_dir="app/drafting_engine/assets/fonts"):
        self.assets_dir = assets_dir
        self.advocate_name = "वी०डी० शुक्ला एडवोकेट"
        self.advocate_location = "बिधूना, औरैया"

    def render_docx(self, draft_schema: dict, output_path: str) -> str:
        doc = Document()
        
        # CRITICAL: Enforce Legal Size (8.5 x 14) and Advocate Formatting Rules
        for section in doc.sections:
            section.page_width = Inches(8.5)
            section.page_height = Inches(14.0)
            section.left_margin = Inches(1.25)
            section.right_margin = Inches(1.25)
            section.top_margin = Inches(1.0)
            section.bottom_margin = Inches(1.0)

        # Base Styles
        style = doc.styles['Normal']
        style.font.name = 'Mangal' # Preferred Unicode Target
        style.font.size = Pt(14)
        style.paragraph_format.line_spacing = 1.5
        style.paragraph_format.space_after = Pt(12)

        # 1. Court Heading
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(draft_schema.get('court_name', 'न्यायालय [अज्ञात]'))
        run.bold = True
        run.font.size = Pt(16)

        # 2. Party Block
        doc.add_paragraph(f"{draft_schema.get('plaintiffs', 'वादी [रिक्त]')} ................... वादी")
        
        p = doc.add_paragraph("बनाम")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        doc.add_paragraph(f"{draft_schema.get('defendants', 'प्रतिवादी [रिक्त]')} ............... प्रतिवादी")
        
        # 3. Title
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(draft_schema.get('nature_of_suit', 'वाद पत्र'))
        run.bold = True
        run.underline = True
        
        # 4. Substantive Averments (Forced Sequential Numbering)
        doc.add_paragraph("वादी निम्नानुसार निवेदन करता है:")
        
        fact_counter = 1
        for averment in draft_schema.get('factual_averments', []):
            p = doc.add_paragraph(f"{fact_counter}. यह कि {averment}")
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            fact_counter += 1
            
        # 5. Technical Sections
        if 'cause_of_action' in draft_schema:
            p = doc.add_paragraph(f"{fact_counter}. यह कि वाद कारण {draft_schema['cause_of_action']}")
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            fact_counter += 1

        # 6. Prayer Block (Relief separated from facts)
        prayer_clauses = draft_schema.get('prayer', [])
        if prayer_clauses:
            doc.add_paragraph("अतः वादी माननीय न्यायालय से प्रार्थना करता है कि:")
            for clause in prayer_clauses:
                p = doc.add_paragraph(clause)
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        # 7. Fixed Advocate Block
        doc.add_paragraph(f"स्थान: {self.advocate_location}")
        doc.add_paragraph("दिनांक: ....................")
        
        p = doc.add_paragraph("द्वारा अधिवक्ता-")
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p = doc.add_paragraph(self.advocate_name)
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        doc.save(output_path)
        return output_path
