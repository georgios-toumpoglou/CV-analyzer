from flask import Flask, render_template, request, redirect, url_for, flash
import os
import json
import anthropic
import fitz          # PyMuPDF  — PDF parsing
from docx import Document  # python-docx — DOCX parsing
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max
ALLOWED_EXTENSIONS = {'pdf', 'docx'}

# Anthropic client
client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(file_bytes):
    """Extract plain text from a PDF file (bytes)."""
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    text = ''
    for page in doc:
        text += page.get_text()
    doc.close()
    return text.strip()


def extract_text_from_docx(file_bytes):
    """Extract plain text from a DOCX file (bytes)."""
    import io
    doc = Document(io.BytesIO(file_bytes))
    text = '\n'.join([para.text for para in doc.paragraphs if para.text.strip()])
    return text.strip()


def build_prompt(cv_text, job_description=None):
    """Build the prompt to send to Claude."""

    job_section = ''
    if job_description and job_description.strip():
        job_section = f"""
The user has also provided the following job description. In addition to the general analysis,
include a "job_match" object in your response with:
- "score": an integer 0-100 representing how well the CV matches this role
- "summary": 1-2 sentences explaining the match score and the most critical gaps

JOB DESCRIPTION:
{job_description.strip()}
"""

    prompt = f"""You are an expert CV reviewer and career coach. Analyze the following CV thoroughly and return ONLY a valid JSON object — no preamble, no markdown, no extra text.

The JSON must have exactly this structure:
{{
  "scores": {{
    "overall": <integer 0-100>,
    "impact": <integer 0-100>,
    "brevity": <integer 0-100>,
    "style": <integer 0-100>,
    "skills": <integer 0-100>,
    "structure": <integer 0-100>
  }},
  "categories": [
    {{
      "name": "Impact",
      "icon": "graph-up-arrow",
      "score": <integer 0-100>,
      "positives": ["<specific observation>", ...],
      "negatives": ["<specific observation>", ...]
    }},
    {{
      "name": "Brevity",
      "icon": "scissors",
      "score": <integer 0-100>,
      "positives": ["<specific observation>", ...],
      "negatives": ["<specific observation>", ...]
    }},
    {{
      "name": "Style",
      "icon": "pen",
      "score": <integer 0-100>,
      "positives": ["<specific observation>", ...],
      "negatives": ["<specific observation>", ...]
    }},
    {{
      "name": "Skills",
      "icon": "tools",
      "score": <integer 0-100>,
      "positives": ["<specific observation>", ...],
      "negatives": ["<specific observation>", ...]
    }},
    {{
      "name": "Structure",
      "icon": "layout-text-sidebar",
      "score": <integer 0-100>,
      "positives": ["<specific observation>", ...],
      "negatives": ["<specific observation>", ...]
    }}
  ],
  "recommendations": "<A concise paragraph (3-5 sentences) summarizing the most important changes the user should make to significantly improve their CV.>"
}}

Scoring guide:
- Impact: Are achievements quantified with numbers/metrics? Strong action verbs? Results-driven bullets?
- Brevity: Are bullets concise? Any repetition, filler words, or overly long sentences?
- Style: Active voice throughout? Free of buzzwords and cliches? Grammar and spelling correct?
- Skills: Are relevant hard and soft skills listed? Are they specific or vague?
- Structure: Are all essential sections present (contact info, experience, education, skills)? Logical order?

Provide 2-4 specific, actionable observations per category (both positives and negatives).
The overall score should reflect the weighted average of the five scores.
Be honest, direct, and specific — reference actual content from the CV.
Always respond in English regardless of the CV language.
{job_section}

CV TEXT:
{cv_text}
"""
    return prompt


def analyze_cv(cv_text, job_description=None):
    """Call Claude API and return parsed JSON result."""
    prompt = build_prompt(cv_text, job_description)

    message = client.messages.create(
        model='claude-sonnet-4-5',
        max_tokens=1500,
        messages=[{'role': 'user', 'content': prompt}]
    )

    raw = message.content[0].text.strip()

    # Strip any accidental markdown fences
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    # 1. Check file presence
    if 'cv_file' not in request.files:
        flash('No file uploaded.', 'error')
        return redirect(url_for('index'))

    file = request.files['cv_file']

    if file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('index'))

    if not allowed_file(file.filename):
        flash('Invalid file type. Please upload a PDF or DOCX.', 'error')
        return redirect(url_for('index'))

    # 2. Read file bytes (no saving to disk)
    file_bytes = file.read()
    filename = file.filename
    ext = filename.rsplit('.', 1)[1].lower()

    # 3. Extract text
    if ext == 'pdf':
        cv_text = extract_text_from_pdf(file_bytes)
    else:
        cv_text = extract_text_from_docx(file_bytes)

    if not cv_text or len(cv_text.strip()) < 50:
        flash('Could not extract text from your file. Please check the file is not empty or image-only.', 'error')
        return redirect(url_for('index'))

    # 4. Get optional job description
    job_description = request.form.get('job_description', '').strip() or None

    # 5. Call Claude
    try:
        result = analyze_cv(cv_text, job_description)
    except json.JSONDecodeError:
        flash('The AI returned an unexpected response. Please try again.', 'error')
        return redirect(url_for('index'))
    except Exception as e:
        flash(f'Analysis failed: {str(e)}', 'error')
        return redirect(url_for('index'))

    # 6. Render results
    return render_template(
        'result.html',
        scores=result['scores'],
        categories=result['categories'],
        recommendations=result.get('recommendations', ''),
        job_match=result.get('job_match', None),
        filename=filename,
        analysis_date=datetime.now().strftime('%d %B %Y, %H:%M')
    )


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
