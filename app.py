import streamlit as st
import anthropic
import json
import io
import re

# ─── Page Configuration ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="Term Sheet Scanner",
    page_icon="📄",
    layout="wide"
)

# ─── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-header {
        text-align: center;
        padding: 1.5rem 0 0.5rem 0;
    }
    .main-header h1 {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0.3rem;
    }
    .main-header p {
        color: #666;
        font-size: 1rem;
    }
    .summary-box {
        background-color: #f8f9fa;
        border-left: 4px solid #185FA5;
        border-radius: 4px;
        padding: 1rem 1.25rem;
        margin-bottom: 1.5rem;
    }
    .disclaimer {
        text-align: center;
        color: #999;
        font-size: 0.8rem;
        margin-top: 2rem;
        padding-top: 1rem;
        border-top: 1px solid #eee;
    }
    .stDataFrame { font-size: 13px; }
    div[data-testid="stFileUploader"] { border: 2px dashed #185FA5; border-radius: 8px; padding: 1rem; }
</style>
""", unsafe_allow_html=True)

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the Debt Capital Markets Head of a mid-sized NBFC with an AUM of Rs 300 Cr \
and 10-15 years of experience raising debt for NBFCs. The NBFC operates in consumer lending — \
personal loans, small business loans, and LAP — and borrows from banks and larger NBFCs for \
onward lending and working capital.

Analyse the term sheet provided. Your purpose is to identify clauses that are onerous, \
detrimental, difficult to comply with, irrelevant to the NBFC's business, or likely to \
constrain future growth — and recommend specific modifications for negotiation with the lender.

STEP 1: Check if the document is actually a term sheet or lending agreement.
If it is NOT a term sheet, respond ONLY with this exact JSON: {"not_termsheet": true}

STEP 2: If it IS a term sheet, respond ONLY with valid JSON in this exact format \
(no markdown, no backticks, no preamble, no text outside the JSON):

{
  "summary": {
    "overall": "One sentence overall assessment of the term sheet",
    "non_negotiable_count": 0,
    "most_critical": "Name of the single most critical clause requiring modification"
  },
  "clauses": [
    {
      "clause_name": "Short name of the clause",
      "clause_details": "Exact or near-exact clause language from the term sheet",
      "implications": "Specific problem this clause creates. Note whether it is tighter than market standard, in line with market, or more relaxed than market for NBFCs of similar profile.",
      "criticality": "Non-negotiable",
      "lenders_pushback": "The argument the lender will most likely make against the requested modification, followed by the counter to it.",
      "revised_clause": "The exact revised clause language to propose to the lender",
      "rationale": "Specific rationale calling out the constraint the original clause creates, how the revised clause addresses it, and how the lender's underlying risk concern is still protected."
    }
  ]
}

Criticality must be EXACTLY one of: Non-negotiable, Can live with it, Good to have
Sort order: Non-negotiable first, then Can live with it, then Good to have.
Within each tier, sort by likely lender resistance — easiest concessions first.
Be specific to the NBFC's profile. Avoid generic observations."""

# ─── Text Extraction Functions ────────────────────────────────────────────────
def extract_from_pdf(file_bytes):
    """Extract text from PDF using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text.strip()
    except ImportError:
        st.error("PyMuPDF not installed. Please add 'pymupdf' to requirements.txt")
        return None
    except Exception as e:
        st.error(f"Error reading PDF: {str(e)}")
        return None

def extract_from_docx(file_bytes):
    """Extract text from Word document."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        text = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
        return text.strip()
    except ImportError:
        st.error("python-docx not installed. Please add 'python-docx' to requirements.txt")
        return None
    except Exception as e:
        st.error(f"Error reading Word document: {str(e)}")
        return None

def extract_from_excel(file_bytes):
    """Extract text from Excel file."""
    try:
        import pandas as pd
        dfs = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
        text = ""
        for sheet_name, df in dfs.items():
            text += f"\n--- Sheet: {sheet_name} ---\n"
            text += df.to_string(index=False)
        return text.strip()
    except ImportError:
        st.error("pandas/openpyxl not installed. Please add 'pandas' and 'openpyxl' to requirements.txt")
        return None
    except Exception as e:
        st.error(f"Error reading Excel file: {str(e)}")
        return None

def extract_from_image(file_bytes, filename):
    """Extract text from image using Claude's vision capability."""
    try:
        import base64
        ext = filename.split('.')[-1].lower()
        media_type_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}
        media_type = media_type_map.get(ext, 'image/jpeg')
        image_data = base64.standard_b64encode(file_bytes).decode("utf-8")
        return {"type": "image", "data": image_data, "media_type": media_type}
    except Exception as e:
        st.error(f"Error processing image: {str(e)}")
        return None

def extract_text(uploaded_file):
    """Route file to correct extraction function based on type."""
    file_bytes = uploaded_file.read()
    filename = uploaded_file.name.lower()

    if filename.endswith('.pdf'):
        return extract_from_pdf(file_bytes), "text"
    elif filename.endswith('.docx') or filename.endswith('.doc'):
        return extract_from_docx(file_bytes), "text"
    elif filename.endswith('.xlsx') or filename.endswith('.xls'):
        return extract_from_excel(file_bytes), "text"
    elif filename.endswith(('.jpg', '.jpeg', '.png')):
        return extract_from_image(file_bytes, filename), "image"
    else:
        return None, "unsupported"

# ─── Claude API Call ──────────────────────────────────────────────────────────
def analyse_term_sheet(content, content_type, api_key):
    """Send term sheet to Claude API and return parsed JSON response."""
    try:
        client = anthropic.Anthropic(api_key=api_key)

        if content_type == "image":
            user_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": content["media_type"],
                        "data": content["data"],
                    },
                },
                {
                    "type": "text",
                    "text": "Please analyse this term sheet image according to your instructions."
                }
            ]
        else:
            # Limit text to 6000 chars to stay within token budget
            truncated = content[:6000]
            if len(content) > 6000:
                truncated += "\n\n[Document truncated for analysis]"
            user_content = f"Please analyse this term sheet:\n\n{truncated}"

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}]
        )

        raw = message.content[0].text.strip()

        # Clean up any accidental markdown wrapping
        clean = re.sub(r'```json|```', '', raw).strip()

        parsed = json.loads(clean)
        return parsed, None

    except json.JSONDecodeError as e:
        return None, f"Analysis could not be processed (JSON parse error). Please try again. Detail: {str(e)}"
    except anthropic.AuthenticationError:
        return None, "Invalid API key. Please check your Anthropic API key in Streamlit secrets."
    except anthropic.RateLimitError:
        return None, "Rate limit reached. Please wait a moment and try again."
    except Exception as e:
        return None, f"Analysis failed: {str(e)}"

# ─── Display Functions ─────────────────────────────────────────────────────────
def display_summary(summary):
    """Display the executive summary box."""
    nn_count = summary.get('non_negotiable_count', 0)
    st.markdown(f"""
    <div class="summary-box">
        <strong>Overall Assessment:</strong> {summary.get('overall', 'N/A')}<br>
        <strong>Non-negotiable changes required:</strong> {nn_count}<br>
        <strong>Most critical clause:</strong> {summary.get('most_critical', 'N/A')}
    </div>
    """, unsafe_allow_html=True)

def get_criticality_badge(criticality):
    """Return colour-coded badge HTML for criticality."""
    colours = {
        "Non-negotiable": ("🔴", "#A32D2D", "#FCEBEB"),
        "Can live with it": ("🟡", "#854F0B", "#FAEEDA"),
        "Good to have": ("🟢", "#3B6D11", "#EAF3DE")
    }
    emoji, color, bg = colours.get(criticality, ("⚪", "#333", "#f0f0f0"))
    return f'<span style="background:{bg};color:{color};padding:3px 8px;border-radius:12px;font-size:11px;font-weight:600;">{emoji} {criticality}</span>'

def display_results_table(clauses):
    """Display the analysis results as a formatted table."""
    import pandas as pd

    # Build dataframe
    rows = []
    for c in clauses:
        rows.append({
            "Clause Name": c.get("clause_name", ""),
            "Clause Details": c.get("clause_details", ""),
            "Implications": c.get("implications", ""),
            "Criticality": c.get("criticality", ""),
            "Lender's Likely Pushback": c.get("lenders_pushback", ""),
            "Revised Clause": c.get("revised_clause", ""),
            "Rationale for Modification": c.get("rationale", "")
        })

    df = pd.DataFrame(rows)

    # Display with colour coding by criticality
    def highlight_criticality(row):
        if row['Criticality'] == 'Non-negotiable':
            return ['background-color: #fff0f0'] * len(row)
        elif row['Criticality'] == 'Can live with it':
            return ['background-color: #fffbf0'] * len(row)
        else:
            return ['background-color: #f0fff0'] * len(row)

    styled_df = df.style.apply(highlight_criticality, axis=1)

    st.dataframe(
        styled_df,
        use_container_width=True,
        height=500,
        column_config={
            "Clause Name": st.column_config.TextColumn(width="small"),
            "Clause Details": st.column_config.TextColumn(width="medium"),
            "Implications": st.column_config.TextColumn(width="medium"),
            "Criticality": st.column_config.TextColumn(width="small"),
            "Lender's Likely Pushback": st.column_config.TextColumn(width="medium"),
            "Revised Clause": st.column_config.TextColumn(width="medium"),
            "Rationale for Modification": st.column_config.TextColumn(width="large"),
        }
    )
    return df

def generate_excel_download(df):
    """Generate Excel file for download."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Term Sheet Analysis"

        # Header styling
        header_fill = PatternFill(start_color="185FA5", end_color="185FA5", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Criticality colours
        crit_colours = {
            "Non-negotiable": "FCEBEB",
            "Can live with it": "FAEEDA",
            "Good to have": "EAF3DE"
        }

        # Write headers
        headers = list(df.columns)
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            ws.row_dimensions[1].height = 30

        # Write data rows
        for row_idx, row in df.iterrows():
            criticality = row.get('Criticality', '')
            row_colour = crit_colours.get(criticality, "FFFFFF")
            row_fill = PatternFill(start_color=row_colour, end_color=row_colour, fill_type="solid")
            border = Border(
                left=Side(style='thin', color='CCCCCC'),
                right=Side(style='thin', color='CCCCCC'),
                top=Side(style='thin', color='CCCCCC'),
                bottom=Side(style='thin', color='CCCCCC')
            )
            for col_idx, value in enumerate(row, 1):
                cell = ws.cell(row=row_idx + 2, column=col_idx, value=str(value))
                cell.fill = row_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.border = border

        # Column widths
        col_widths = [18, 30, 30, 16, 30, 30, 35]
        for i, width in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = width

        # Save to bytes
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output.getvalue()

    except ImportError:
        # Fallback to CSV if openpyxl not available
        csv_data = df.to_csv(index=False)
        return csv_data.encode('utf-8-sig')

# ─── Main App ─────────────────────────────────────────────────────────────────
def main():
    # Header
    st.markdown("""
    <div class="main-header">
        <h1>📄 Term Sheet Scanner</h1>
        <p>AI-powered term sheet analysis for NBFC debt capital markets teams</p>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # ── API Key Setup ──
    # Try Streamlit secrets first, then fall back to sidebar input
    api_key = None
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        with st.sidebar:
            st.markdown("### ⚙️ Configuration")
            api_key = st.text_input(
                "Anthropic API Key",
                type="password",
                placeholder="sk-ant-...",
                help="Enter your Anthropic API key. Get one at console.anthropic.com"
            )
            if api_key:
                st.success("API key entered ✓")
            else:
                st.warning("Please enter your API key to use the scanner.")

    # ── File Upload Section ──
    st.markdown("### Upload Term Sheet")
    st.caption("Supported formats: PDF, Word (.docx), Excel (.xlsx), Image (JPG, PNG) · Maximum file size: 10MB")

    uploaded_file = st.file_uploader(
        "Drop your term sheet here or click to browse",
        type=["pdf", "docx", "doc", "xlsx", "xls", "jpg", "jpeg", "png"],
        label_visibility="collapsed"
    )

    if uploaded_file:
        # File size check (10MB)
        if uploaded_file.size > 10 * 1024 * 1024:
            st.error("File size exceeds 10MB limit. Please upload a smaller file.")
            st.stop()

        file_size_kb = uploaded_file.size / 1024
        st.success(f"✓ File selected: **{uploaded_file.name}** ({file_size_kb:.1f} KB)")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            analyse_clicked = st.button(
                "🔍 Analyse Term Sheet",
                type="primary",
                use_container_width=True,
                disabled=not api_key
            )

        if not api_key:
            st.info("Please enter your Anthropic API key in the sidebar to enable analysis.")

        if analyse_clicked and api_key:
            # ── Text Extraction ──
            with st.spinner("Extracting text from document..."):
                content, content_type = extract_text(uploaded_file)

            if content_type == "unsupported":
                st.error("Error: File format not supported. Please upload a PDF, Word, Excel, or Image file.")
                st.stop()

            if content is None:
                st.error("Could not extract text from the file. Please ensure the file is not corrupted and try again.")
                st.stop()

            if content_type == "text" and len(str(content).strip()) < 50:
                st.error("Could not extract readable text from this file. If this is a scanned image, please save it as JPG or PNG and upload as an image file.")
                st.stop()

            # ── Claude API Call ──
            with st.spinner("Analysing the term sheet — this may take 15–30 seconds..."):
                result, error = analyse_term_sheet(content, content_type, api_key)

            if error:
                st.error(f"Analysis error: {error}")
                st.stop()

            # ── Display Results ──
            st.divider()
            st.markdown("### Analysed Term Sheet")

            if result.get("not_termsheet"):
                st.warning("⚠️ Document does not appear to be a term sheet. Please upload the correct document.")
                if st.button("🔄 Try Again"):
                    st.rerun()
                st.stop()

            # Summary
            if "summary" in result:
                display_summary(result["summary"])

            # Results Table
            clauses = result.get("clauses", [])
            if not clauses:
                st.warning("No clauses were identified for modification. The term sheet may be broadly market standard.")
                st.stop()

            # Clause count summary
            nn = sum(1 for c in clauses if c.get("criticality") == "Non-negotiable")
            cl = sum(1 for c in clauses if c.get("criticality") == "Can live with it")
            gh = sum(1 for c in clauses if c.get("criticality") == "Good to have")

            col1, col2, col3 = st.columns(3)
            col1.metric("🔴 Non-negotiable", nn)
            col2.metric("🟡 Can live with it", cl)
            col3.metric("🟢 Good to have", gh)

            st.markdown("&nbsp;")
            df = display_results_table(clauses)

            # ── Download Section ──
            st.divider()
            col1, col2 = st.columns([1, 3])

            with col1:
                excel_data = generate_excel_download(df)
                file_ext = "xlsx" if isinstance(excel_data, bytes) and excel_data[:4] == b'PK\x03\x04' else "csv"
                st.download_button(
                    label="⬇️ Download Analysis",
                    data=excel_data,
                    file_name=f"term_sheet_analysis_{uploaded_file.name.split('.')[0]}.{file_ext}",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    use_container_width=True
                )

            with col2:
                if st.button("🔄 Analyse Another Term Sheet", use_container_width=False):
                    st.rerun()

    # ── Disclaimer ──
    st.markdown("""
    <div class="disclaimer">
        In MVP-1, analysis is not persisted. If the user closes the browser, the analysis is lost.
        The user must download the file before closing. MVP-1 has no authentication — anyone with the URL can access the tool.
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
