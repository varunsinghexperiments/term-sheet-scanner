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
    .main-header { text-align: center; padding: 1.5rem 0 0.5rem 0; }
    .main-header h1 { font-size: 2.2rem; font-weight: 700; color: #1a1a2e; margin-bottom: 0.3rem; }
    .main-header p { color: #666; font-size: 1rem; }
    .summary-box {
        background-color: #f8f9fa;
        border-left: 4px solid #185FA5;
        border-radius: 4px;
        padding: 1rem 1.25rem;
        margin-bottom: 1.5rem;
    }
    .disclaimer {
        text-align: center; color: #999; font-size: 0.8rem;
        margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #eee;
    }
    div[data-testid="stFileUploader"] { border: 2px dashed #185FA5; border-radius: 8px; padding: 1rem; }

    /* ── Compact table styles ── */
    .compact-table-wrapper {
        overflow-x: auto;
        overflow-y: auto;
        max-height: 520px;
        border: 1px solid #d0d0d0;
        border-radius: 6px;
        margin-bottom: 1rem;
    }
    .compact-table {
        border-collapse: collapse;
        width: 100%;
        font-size: 12.5px;
        font-family: sans-serif;
        white-space: nowrap;
    }
    .compact-table thead th {
        background-color: #185FA5;
        color: white;
        padding: 8px 12px;
        text-align: left;
        font-weight: 600;
        position: sticky;
        top: 0;
        z-index: 2;
        white-space: nowrap;
    }
    .compact-table tbody tr { border-bottom: 1px solid #e8e8e8; }
    .compact-table tbody tr:hover { filter: brightness(0.96); }
    .compact-table tbody td {
        padding: 6px 12px;
        vertical-align: middle;
        max-width: 220px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        cursor: default;
    }
    .compact-table tbody td:first-child { font-weight: 600; min-width: 130px; }
    .row-nn { background-color: #fff0f0; }
    .row-cl { background-color: #fffbf0; }
    .row-gh { background-color: #f0fff0; }
    .badge {
        display: inline-block; font-size: 10.5px; padding: 2px 8px;
        border-radius: 12px; font-weight: 600; white-space: nowrap;
    }
    .badge-nn { background: #FCEBEB; color: #A32D2D; }
    .badge-cl { background: #FAEEDA; color: #854F0B; }
    .badge-gh { background: #EAF3DE; color: #3B6D11; }
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
      "implications": "Specific problem this clause creates. Note whether tighter/in line/more relaxed than market standard.",
      "criticality": "Non-negotiable",
      "lenders_pushback": "The argument the lender will most likely make, followed by the counter to it.",
      "revised_clause": "The exact revised clause language to propose to the lender",
      "rationale": "Specific rationale calling out the constraint, how the revised clause addresses it, and how lender risk is still protected."
    }
  ]
}

Criticality must be EXACTLY one of: Non-negotiable, Can live with it, Good to have
Sort order: Non-negotiable first, then Can live with it, then Good to have.
Within each tier, sort by likely lender resistance — easiest concessions first.
Be specific to the NBFC's profile. Avoid generic observations.
Keep each field concise — maximum 60 words per field. Analyse only the most significant clauses (max 12)."""

# ─── Text Extraction Functions ────────────────────────────────────────────────
def extract_from_pdf(file_bytes):
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text.strip()
    except ImportError:
        st.error("PyMuPDF not installed. Add 'pymupdf' to requirements.txt")
        return None
    except Exception as e:
        st.error(f"Error reading PDF: {e}")
        return None

def extract_from_docx(file_bytes):
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        st.error("python-docx not installed. Add 'python-docx' to requirements.txt")
        return None
    except Exception as e:
        st.error(f"Error reading Word doc: {e}")
        return None

def extract_from_excel(file_bytes):
    try:
        import pandas as pd
        dfs = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
        return "\n".join(f"\n--- Sheet: {k} ---\n{v.to_string(index=False)}" for k, v in dfs.items())
    except Exception as e:
        st.error(f"Error reading Excel: {e}")
        return None

def extract_from_image(file_bytes, filename):
    try:
        import base64
        ext = filename.split('.')[-1].lower()
        media_type = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}.get(ext, 'image/jpeg')
        return {"type": "image", "data": base64.standard_b64encode(file_bytes).decode("utf-8"), "media_type": media_type}
    except Exception as e:
        st.error(f"Error processing image: {e}")
        return None

def extract_text(uploaded_file):
    file_bytes = uploaded_file.read()
    filename = uploaded_file.name.lower()
    if filename.endswith('.pdf'):
        return extract_from_pdf(file_bytes), "text"
    elif filename.endswith(('.docx', '.doc')):
        return extract_from_docx(file_bytes), "text"
    elif filename.endswith(('.xlsx', '.xls')):
        return extract_from_excel(file_bytes), "text"
    elif filename.endswith(('.jpg', '.jpeg', '.png')):
        return extract_from_image(file_bytes, filename), "image"
    return None, "unsupported"

# ─── Claude API Call ──────────────────────────────────────────────────────────
def analyse_term_sheet(content, content_type, api_key):
    try:
        client = anthropic.Anthropic(api_key=api_key)
        if content_type == "image":
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": content["media_type"], "data": content["data"]}},
                {"type": "text", "text": "Please analyse this term sheet image according to your instructions."}
            ]
        else:
            truncated = content[:6000] + ("\n\n[Document truncated]" if len(content) > 6000 else "")
            user_content = f"Please analyse this term sheet:\n\n{truncated}"

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}]
        )
        raw = message.content[0].text.strip()
        clean = re.sub(r'```json|```', '', raw).strip()
        return json.loads(clean), None
    except json.JSONDecodeError as e:
        return None, f"Analysis could not be processed (JSON parse error). Please try again. Detail: {e}"
    except anthropic.AuthenticationError:
        return None, "Invalid API key. Please check your Anthropic API key in Streamlit secrets."
    except anthropic.RateLimitError:
        return None, "Rate limit reached. Please wait a moment and try again."
    except Exception as e:
        return None, f"Analysis failed: {e}"

# ─── Display Functions ────────────────────────────────────────────────────────
def display_summary(summary):
    st.markdown(f"""
    <div class="summary-box">
        <strong>Overall Assessment:</strong> {summary.get('overall','N/A')}<br>
        <strong>Non-negotiable changes required:</strong> {summary.get('non_negotiable_count',0)}<br>
        <strong>Most critical clause:</strong> {summary.get('most_critical','N/A')}
    </div>""", unsafe_allow_html=True)

def display_compact_table(clauses):
    """Render a compact, single-line-per-row HTML table like Image 2."""
    def badge(c):
        if c == "Non-negotiable":
            return '<span class="badge badge-nn">🔴 Non-negotiable</span>'
        elif c == "Can live with it":
            return '<span class="badge badge-cl">🟡 Can live with it</span>'
        return '<span class="badge badge-gh">🟢 Good to have</span>'

    def row_class(c):
        return {"Non-negotiable": "row-nn", "Can live with it": "row-cl", "Good to have": "row-gh"}.get(c, "")

    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

    headers = ["Clause Name","Clause Details","Implications","Criticality",
               "Lender's Likely Pushback","Revised Clause","Rationale for Modification"]

    rows_html = ""
    for c in clauses:
        vals = [
            esc(c.get("clause_name","")),
            esc(c.get("clause_details","")),
            esc(c.get("implications","")),
            badge(c.get("criticality","")),
            esc(c.get("lenders_pushback","")),
            esc(c.get("revised_clause","")),
            esc(c.get("rationale",""))
        ]
        rc = row_class(c.get("criticality",""))
        cells = "".join(
            f'<td title="{v}" class="">{v}</td>' if i == 3 else f'<td title="{v}">{v}</td>'
            for i, v in enumerate(vals)
        )
        rows_html += f'<tr class="{rc}">{cells}</tr>'

    header_html = "".join(f"<th>{h}</th>" for h in headers)

    st.markdown(f"""
    <div class="compact-table-wrapper">
      <table class="compact-table">
        <thead><tr>{header_html}</tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <p style="font-size:11px;color:#888;margin-top:-0.5rem;">
      💡 Hover over any cell to see the full text. Download the Excel for the complete analysis.
    </p>""", unsafe_allow_html=True)

def build_dataframe(clauses):
    import pandas as pd
    return pd.DataFrame([{
        "Clause Name": c.get("clause_name",""),
        "Clause Details": c.get("clause_details",""),
        "Implications": c.get("implications",""),
        "Criticality": c.get("criticality",""),
        "Lender's Likely Pushback": c.get("lenders_pushback",""),
        "Revised Clause": c.get("revised_clause",""),
        "Rationale for Modification": c.get("rationale","")
    } for c in clauses])

def generate_excel_download(df):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Term Sheet Analysis"

        header_fill = PatternFill(start_color="185FA5", end_color="185FA5", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        crit_colours = {"Non-negotiable":"FCEBEB","Can live with it":"FAEEDA","Good to have":"EAF3DE"}

        for col_idx, header in enumerate(df.columns, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 30

        border = Border(
            left=Side(style='thin', color='CCCCCC'), right=Side(style='thin', color='CCCCCC'),
            top=Side(style='thin', color='CCCCCC'), bottom=Side(style='thin', color='CCCCCC')
        )
        for row_idx, row in df.iterrows():
            row_colour = crit_colours.get(row.get('Criticality',''), "FFFFFF")
            row_fill = PatternFill(start_color=row_colour, end_color=row_colour, fill_type="solid")
            for col_idx, value in enumerate(row, 1):
                cell = ws.cell(row=row_idx + 2, column=col_idx, value=str(value))
                cell.fill = row_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.border = border

        for i, width in enumerate([18, 30, 30, 16, 30, 30, 35], 1):
            ws.column_dimensions[get_column_letter(i)].width = width

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output.getvalue(), "xlsx"
    except ImportError:
        return df.to_csv(index=False).encode('utf-8-sig'), "csv"

# ─── Main App ─────────────────────────────────────────────────────────────────
def main():
    # ── Session state initialisation ──
    if "analysis_result" not in st.session_state:
        st.session_state.analysis_result = None
    if "analysis_df" not in st.session_state:
        st.session_state.analysis_df = None
    if "analysis_filename" not in st.session_state:
        st.session_state.analysis_filename = None
    if "excel_data" not in st.session_state:
        st.session_state.excel_data = None
    if "excel_ext" not in st.session_state:
        st.session_state.excel_ext = "xlsx"

    # Header
    st.markdown("""
    <div class="main-header">
        <h1>📄 Term Sheet Scanner</h1>
        <p>AI-powered term sheet analysis for NBFC debt capital markets teams</p>
    </div>""", unsafe_allow_html=True)
    st.divider()

    # ── API Key ──
    api_key = None
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        with st.sidebar:
            st.markdown("### ⚙️ Configuration")
            api_key = st.text_input("Anthropic API Key", type="password", placeholder="sk-ant-...")
            if api_key:
                st.success("API key entered ✓")
            else:
                st.warning("Please enter your API key.")

    # ── Upload Section (shown only when no analysis is stored) ──
    if st.session_state.analysis_result is None:
        st.markdown("### Upload Term Sheet")
        st.caption("Supported formats: PDF, Word (.docx), Excel (.xlsx), Image (JPG, PNG) · Maximum file size: 10MB")

        uploaded_file = st.file_uploader(
            "Drop your term sheet here or click to browse",
            type=["pdf","docx","doc","xlsx","xls","jpg","jpeg","png"],
            label_visibility="collapsed"
        )

        if uploaded_file:
            if uploaded_file.size > 10 * 1024 * 1024:
                st.error("File size exceeds 10MB limit. Please upload a smaller file.")
                st.stop()

            st.success(f"✓ File selected: **{uploaded_file.name}** ({uploaded_file.size/1024:.1f} KB)")

            col1, col2, col3 = st.columns([1,1,1])
            with col2:
                analyse_clicked = st.button("🔍 Analyse Term Sheet", type="primary",
                                            use_container_width=True, disabled=not api_key)

            if not api_key:
                st.info("Please enter your Anthropic API key in the sidebar.")

            if analyse_clicked and api_key:
                with st.spinner("Extracting text from document..."):
                    content, content_type = extract_text(uploaded_file)

                if content_type == "unsupported":
                    st.error("File format not supported.")
                    st.stop()
                if content is None:
                    st.error("Could not extract text. Please ensure the file is not corrupted.")
                    st.stop()
                if content_type == "text" and len(str(content).strip()) < 50:
                    st.error("Could not extract readable text. Try uploading as JPG/PNG if it is a scanned document.")
                    st.stop()

                with st.spinner("Analysing the term sheet — this may take 15–30 seconds..."):
                    result, error = analyse_term_sheet(content, content_type, api_key)

                if error:
                    st.error(f"Analysis error: {error}")
                    st.stop()

                if result.get("not_termsheet"):
                    st.warning("⚠️ Document does not appear to be a term sheet. Please upload the correct document.")
                    st.stop()

                # ── Store results in session state ──
                df = build_dataframe(result.get("clauses", []))
                excel_data, excel_ext = generate_excel_download(df)

                st.session_state.analysis_result = result
                st.session_state.analysis_df = df
                st.session_state.analysis_filename = uploaded_file.name.split('.')[0]
                st.session_state.excel_data = excel_data
                st.session_state.excel_ext = excel_ext

                st.rerun()

    # ── Results Section (shown when analysis is stored in session state) ──
    if st.session_state.analysis_result is not None:
        result = st.session_state.analysis_result
        df = st.session_state.analysis_df
        clauses = result.get("clauses", [])

        st.markdown("### Analysed Term Sheet")

        if "summary" in result:
            display_summary(result["summary"])

        nn = sum(1 for c in clauses if c.get("criticality") == "Non-negotiable")
        cl = sum(1 for c in clauses if c.get("criticality") == "Can live with it")
        gh = sum(1 for c in clauses if c.get("criticality") == "Good to have")

        col1, col2, col3 = st.columns(3)
        col1.metric("🔴 Non-negotiable", nn)
        col2.metric("🟡 Can live with it", cl)
        col3.metric("🟢 Good to have", gh)

        st.markdown("&nbsp;")
        display_compact_table(clauses)

        st.divider()
        col1, col2 = st.columns([1, 3])

        with col1:
            ext = st.session_state.excel_ext
            st.download_button(
                label="⬇️ Download Analysis",
                data=st.session_state.excel_data,
                file_name=f"term_sheet_analysis_{st.session_state.analysis_filename}.{ext}",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True
            )

        with col2:
            if st.button("🔄 Analyse Another Term Sheet"):
                st.session_state.analysis_result = None
                st.session_state.analysis_df = None
                st.session_state.analysis_filename = None
                st.session_state.excel_data = None
                st.session_state.excel_ext = "xlsx"
                st.rerun()

    st.markdown("""
    <div class="disclaimer">
        In MVP-1, analysis is not persisted. If the user closes the browser, the analysis is lost.
        The user must download the file before closing. MVP-1 has no authentication — anyone with the URL can access the tool.
    </div>""", unsafe_allow_html=True)

if __name__ == "__main__":
    main()
