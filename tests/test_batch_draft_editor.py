from app.drafting_engine.draft_editor import DraftEditor


def test_batch_and_flexible_edits():
    editor = DraftEditor()
    initial_draft = {
        "title": "वाद पत्र बाबत स्थायी निषेधाज्ञा",
        "pleadings": [
            "यह कि वादी विवादित भूमि गाटा संख्या 214 का स्वामी है।",
            "यह कि प्रतिवादीगण ने दिनांक 15.06.2025 को अनाधिकार धमकी दी।",
        ],
        "signature_block": ["वादी", "द्वारा अधिवक्ता"],
    }

    # Test batch edit
    batch_edit = {
        "operation": "batch",
        "section": "pleadings",
        "index": 0,
        "old_text": "",
        "new_text": "",
        "reason": "Multiple changes",
        "edits": [
            {
                "operation": "replace",
                "section": "pleadings",
                "index": 0,
                "old_text": "गाटा संख्या 214",
                "new_text": "गाटा संख्या 215",
                "reason": "Correct plot number",
            },
            {
                "operation": "replace",
                "section": "pleadings",
                "index": 1,
                "old_text": "15.06.2025",
                "new_text": "20.06.2025",
                "reason": "Correct date",
            },
        ],
    }

    updated, desc = editor.apply(initial_draft, batch_edit)
    assert "गाटा संख्या 215" in updated["pleadings"][0]
    assert "20.06.2025" in updated["pleadings"][1]
    assert "पैराग्राफ 1 में बदलाव" in desc
    assert "पैराग्राफ 2 में बदलाव" in desc


if __name__ == "__main__":
    test_batch_and_flexible_edits()
    print("test_batch_draft_editor passed successfully!")
