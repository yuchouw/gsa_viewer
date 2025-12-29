import sys
import warnings
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import re
import io
import altair as alt


# ==========================================
# 1. PARSING LOGIC
# ==========================================
def parse_gsa_file(file_obj):
    """
    Parses the GSA CSV file. 
    Handles basic text parsing for Input Data, Effective Lengths, and Design Checks.
    Attempts to parse Force/Point tables if they exist in standard GSA table format.
    """
    # Read all lines from the uploaded file
    content = file_obj.getvalue().decode('utf-8', errors='replace')
    lines = content.splitlines()

    # Data Containers
    data = {
        'metadata': {},
        'points': {},
        'forces': [],
        'units': {},  # Store detected units for Fx, Fy, etc.
        'input_data': [],
        'effective_lengths': [],
        'local_checks': [],
        'buckling_checks': []
    }

    # --- Parsing Flags ---
    section = None  # 'INPUT', 'EFFECTIVE', 'LOCAL', 'BUCKLING'
    skip_input_block = False

    # Keywords to identify specific checks
    target_local = [
        "Major axis shear check", "Minor axis shear check",
        "Major axis bending check", "Minor axis bending check",
        "Torsion check", "Combined biaxial bending and compression check",
        "Axial tension check", "Axial compression check", "Combined biaxial bending and tension check"
    ]
    target_buckling = [
        "Check axial buckling major axis", "Check axial buckling minor axis",
        "Check LT buckling", "Check FT buckling"
    ]

    current_check = None
    current_perm = "N/A"  # Track permutation context statefully

    for i, line in enumerate(lines):
        # --- Metadata Extraction ---
        if "Member list:" in line:
            parts = line.split(":", 1)
            if len(parts) > 1: data['metadata']['member'] = parts[1].strip()
        if "Combination Case" in line:
            data['metadata']['combo'] = line.strip()

        # Update current permutation state if line contains it
        if "Envelope permutation" in line:
            parts = line.split(":", 1)
            if len(parts) > 1:
                current_perm = parts[1].strip()
                data['metadata']['perm'] = current_perm  # Keep last seen in metadata

        # --- Pre-processing for Hierarchy ---
        # Count leading commas to determine indentation level
        leading_commas = 0
        for char in line:
            if char == ',':
                leading_commas += 1
            else:
                break

        # Extract content (skipping leading commas)
        raw_content = line[leading_commas:].strip()
        clean_content = raw_content.rstrip(',')

        # FIX: Remove CSV artifact quotes (e.g., "Text" -> Text)
        clean_content = clean_content.strip('"').strip("'")

        # Create a display-friendly line with indentation using TABS
        indent_str = "\t" * leading_commas
        display_line = f"{indent_str}{clean_content}"

        # --- A. Section Detection ---
        if "Input Data:" in clean_content:
            section = 'INPUT'
            continue
        elif "Effective Lengths" in clean_content and "Calculation Overrides" not in clean_content and i > 10:
            section = 'EFFECTIVE'
            continue
        elif "Local capacity checks:" in clean_content:
            section = 'LOCAL'
            if current_check:
                save_check(data, current_check)
                current_check = None
            continue
        elif "Buckling capacity checks:" in clean_content:
            section = 'BUCKLING'
            if current_check:
                save_check(data, current_check)
                current_check = None
            continue

        # --- B. Data Extraction ---

        # 1. Input Data
        if section == 'INPUT':
            if "Effective Lengths" in clean_content:
                section = 'EFFECTIVE'
                skip_input_block = False
                continue

            if "MemberPoints" in clean_content or "SubSpans" in clean_content:
                skip_input_block = True
                continue

            if skip_input_block:
                if "END_TABLE" in clean_content:
                    skip_input_block = False
                continue

            if "Forces, Moments" in clean_content or "Section Data" in clean_content:
                pass
            elif clean_content:
                data['input_data'].append(display_line)

        # 2. Effective Lengths
        elif section == 'EFFECTIVE':
            if "Local capacity checks" in clean_content:
                section = 'LOCAL'
                skip_input_block = False
                continue

            if "MemberPoints" in clean_content or "SubSpans" in clean_content:
                skip_input_block = True
                continue

            if skip_input_block:
                if "END_TABLE" in clean_content:
                    skip_input_block = False
                continue

            elif clean_content:
                data['effective_lengths'].append(display_line)

        # 3. Design Checks (Local & Buckling)
        if section in ['LOCAL', 'BUCKLING']:
            is_new_target = False
            found_name = ""
            targets = target_local if section == 'LOCAL' else target_buckling

            for t in targets:
                if clean_content.startswith(t):
                    is_new_target = True
                    found_name = t
                    break

            if is_new_target:
                if current_check:
                    save_check(data, current_check)

                current_check = {
                    'name': found_name,
                    'group': section,
                    'lines': [],
                    'indent': leading_commas,
                    'util': 0.0,
                    'perm': current_perm
                }
                current_check['lines'].append(display_line)
                continue

            if current_check:
                if leading_commas > current_check['indent']:
                    current_check['lines'].append(display_line)
                else:
                    save_check(data, current_check)
                    current_check = None

    if current_check:
        save_check(data, current_check)

    # --- C. Post-Process: Points & Forces ---
    data['points'] = parse_points(lines)
    # Parse forces AND extract units dynamically
    data['forces'], data['units'] = parse_forces(lines, data['points'])

    # --- D. Post-Process: Calculate Utilization ---
    for c in data['local_checks']:
        pass
    for c in data['buckling_checks']:
        c['util'] = find_util(c['lines'])

    return data


def save_check(data, check):
    if check['group'] == 'LOCAL':
        processed = process_check_parts(check['lines'])

        if processed['is_split']:
            check['split_data'] = processed
            check['util'] = max(processed['lh']['util'], processed['rh']['util'])
        else:
            check['lines'] = processed['lines']
            check['util'] = processed['util']
            check['split_data'] = None

        data['local_checks'].append(check)
    elif check['group'] == 'BUCKLING':
        data['buckling_checks'].append(check)


def parse_points(lines):
    points = {}
    parsing = False
    for i, line in enumerate(lines):
        if "MemberPoints" in line: continue
        if "START_TABLE" in line and i + 1 < len(lines) and "Ref,Pos" in lines[i + 1]:
            parsing = True;
            continue
        if parsing and "END_TABLE" in line:
            parsing = False;
            continue

        if parsing:
            parts = [p for p in line.strip().split(',') if p]
            if len(parts) >= 2 and parts[0].isdigit():
                try:
                    val_str = parts[1].lower().replace('m', '')
                    points[int(parts[0])] = float(val_str)
                except:
                    pass
    return points


def parse_forces(lines, points):
    forces = []
    # Default units
    units = {
        'Fx': 'kN', 'Fy': 'kN', 'Fz': 'kN',
        'Mxx': 'kNm', 'Myy': 'kNm', 'Mzz': 'kNm'
    }
    # Mapping of column index (in parts[1:7]) to unit key
    unit_keys = ['Fx', 'Fy', 'Fz', 'Mxx', 'Myy', 'Mzz']
    units_detected = False

    parsing = False
    for i, line in enumerate(lines):
        if "SubSpans" in line: continue
        if "START_TABLE" in line and i + 1 < len(lines) and "Ref,Fx" in lines[i + 1]:
            parsing = True;
            continue
        if parsing and "END_TABLE" in line:
            parsing = False;
            continue

        if parsing:
            parts = [p for p in line.strip().split(',') if p]
            if len(parts) > 4 and '-' in parts[0] and parts[0][0].isdigit():
                ref = parts[0]
                is_right = 'R' in ref
                ids = re.findall(r'\d+', ref)
                if len(ids) >= 2:
                    idx = int(ids[1]) + 1 if is_right else int(ids[0]) + 1
                    pos = points.get(idx, 0.0)

                    vals = []
                    # Loop through force columns
                    for k, v in enumerate(parts[1:7]):
                        # Robust scientific notation extraction
                        # Match examples: 12.34, -12.34, 1.2e-3, -1.2E+05
                        match = re.search(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", v)

                        if match:
                            num_str = match.group()
                            try:
                                val = float(num_str)
                            except:
                                val = 0.0
                            vals.append(val)

                            # Detect units from the FIRST valid data row we find
                            if not units_detected and k < len(unit_keys):
                                # Extract unit by removing the number
                                # e.g. "-22.60kN" -> "kN"
                                unit_str = v.replace(num_str, '').strip()
                                if unit_str:
                                    units[unit_keys[k]] = unit_str
                        else:
                            vals.append(0.0)

                    if vals:
                        units_detected = True  # Only detect units once

                    forces.append([pos] + vals)

    return forces, units


def extract_float(text):
    """Robust float extractor that handles scientific notation (e.g. '1.2e-3')."""
    # Regex for float with optional scientific notation
    match = re.search(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", text)
    if match:
        try:
            return float(match.group())
        except:
            return 0.0
    return None


def find_util(lines):
    """
    Finds the max utilization ratio.
    """
    for line in lines:
        if "No axial compression" in line:
            return 0.0

    max_util = 0.0
    found_explicit_util = False

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if "Util =" in line or "Util=" in line or "Ratio =" in line:
            found_explicit_util = True
            current_val = 0.0

            parts = line.split('=')
            if len(parts) > 1:
                val = extract_float(parts[-1])
                if val is not None: current_val = val

            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if next_line.startswith('='):
                    val = extract_float(next_line)
                    if val is not None:
                        current_val = val
                    j += 1
                else:
                    break

            max_util = max(max_util, current_val)
            i = j
        else:
            i += 1

    if not found_explicit_util and lines:
        for line in reversed(lines):
            clean = line.strip()
            if not clean: continue

            if '=' in clean:
                parts = clean.split('=')
                val = extract_float(parts[-1])
                if val is not None:
                    max_util = val
                break

    return max_util


def process_check_parts(lines):
    """
    Detects if lines contain both LH and RH ends.
    """
    lh_idx = -1
    rh_idx = -1

    for i, line in enumerate(lines):
        clean = line.strip()
        if (clean.startswith("LH end") or clean.startswith("Left Hand")) and lh_idx == -1:
            lh_idx = i
        if (clean.startswith("RH end") or clean.startswith("Right Hand")) and rh_idx == -1:
            rh_idx = i

    if lh_idx != -1 and rh_idx != -1:
        first_idx = min(lh_idx, rh_idx)
        second_idx = max(lh_idx, rh_idx)

        header = lines[:first_idx]
        part1 = lines[first_idx:second_idx]
        part2 = lines[second_idx:]

        if lh_idx == first_idx:
            lh_lines = part1
            rh_lines = part2
        else:
            rh_lines = part1
            lh_lines = part2

        return {
            'is_split': True,
            'header': header,
            'lh': {'lines': lh_lines, 'util': find_util(lh_lines)},
            'rh': {'lines': rh_lines, 'util': find_util(rh_lines)}
        }

    return {
        'is_split': False,
        'lines': lines,
        'util': find_util(lines)
    }


# ==========================================
# 2. STREAMLIT UI HELPER
# ==========================================
def render_check_lines(lines, util_val):
    if not lines:
        return

    valid_lines = [l for l in lines if l.strip()]
    min_tabs = min([l.count('\t') for l in valid_lines]) if valid_lines else 0

    for line in lines:
        clean_text = line.strip()
        if not clean_text: continue

        tabs = line.count('\t')
        visual_indent = max(0, tabs - min_tabs)
        indent_px = visual_indent * 15

        if "No axial compression" in clean_text:
            processed_text = f"<b>{clean_text}</b>"
        else:
            def repl(m):
                try:
                    v = float(m.group())
                    if util_val > 0.0001 and abs(v - util_val) < 0.0001:
                        style_props = "font-weight: bold;"
                        if v > 1.0:
                            style_props += " color: red;"
                        elif v > 0.9:
                            style_props += " color: magenta;"
                        return f"<span style='{style_props}'>{m.group()}</span>"
                except:
                    pass
                return m.group()

            processed_text = re.sub(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", repl, clean_text)

        st.markdown(
            f"<div style='padding-left: {indent_px}px; font-size: 0.9rem; margin-bottom: 2px;'>{processed_text}</div>",
            unsafe_allow_html=True
        )


def plot_with_zero_line(df, y_cols, colors, y_title="Value"):
    chart_data = df.melt(id_vars=['Pos'], value_vars=y_cols, var_name='Type', value_name='Value')

    lines = alt.Chart(chart_data).mark_line().encode(
        x=alt.X('Pos', title='Position (m)'),
        y=alt.Y('Value', title=y_title),
        color=alt.Color('Type', scale=alt.Scale(domain=y_cols, range=colors),
                        legend=alt.Legend(title=None, orient='bottom')),
        tooltip=['Pos', 'Type', 'Value']
    )

    rule = alt.Chart(pd.DataFrame({'y': [0]})).mark_rule(color='#666666', strokeWidth=1).encode(y='y')

    return (lines + rule).interactive()


# ==========================================
# 3. STREAMLIT UI
# ==========================================
st.set_page_config(page_title="GSA Design Viewer", layout="wide")

st.title("GSA Design Results Viewer")
st.markdown("""
This tool visualizes **Oasys GSA Member Check** CSV files.
Upload your exported CSV file (Design > Output > Export or Save as CSV) to view parsed checks and diagrams.
""")

uploaded_file = st.file_uploader("Upload CSV", type="csv")

if uploaded_file:
    with st.spinner("Parsing file..."):
        data = parse_gsa_file(uploaded_file)

    # --- PRINT CONTROLS ---
    col_p1, col_p2 = st.columns([1, 4])
    with col_p1:
        if st.button("🖨️ Print to PDF"):
            components.html("<script>window.print();</script>", height=0, width=0)

    with col_p2:
        expand_all = st.checkbox("Expand all details (for printing)", value=False)

    # Inject CSS for printing
    st.markdown("""
        <style>
            @media print {
                html, body {
                    height: auto !important;
                    overflow: visible !important;
                }
                .stApp {
                    height: auto !important;
                    overflow: visible !important;
                }
                .block-container {
                    padding: 0 !important;
                    margin: 0 !important;
                    max-width: 100% !important;
                    overflow: visible !important;
                }
                #MainMenu, header, footer, [data-testid="stSidebar"] {
                    display: none !important;
                }
                .stFileUploader, .stButton, [data-testid="stCheckbox"] {
                    display: none !important;
                }
                * {
                    -webkit-print-color-adjust: exact !important;
                    print-color-adjust: exact !important;
                }
            }
        </style>
    """, unsafe_allow_html=True)

    # --- DASHBOARD SUMMARY ---
    total_local = len(data['local_checks'])
    total_buckling = len(data['buckling_checks'])

    max_local = max([c['util'] for c in data['local_checks']]) if total_local > 0 else 0.0
    max_buckl = max([c['util'] for c in data['buckling_checks']]) if total_buckling > 0 else 0.0

    member_val = data['metadata'].get('member', 'N/A')
    combo_val = data['metadata'].get('combo', 'Combination Case: N/A')

    # Top Row Metrics
    col_sum1, col_sum2, col_sum3 = st.columns(3)
    col_sum1.metric("Member List", member_val)
    col_sum2.metric("Max Local Util", f"{max_local:.3f}", delta_color="inverse" if max_local > 1.0 else "normal")
    col_sum3.metric("Max Buckling Util", f"{max_buckl:.3f}", delta_color="inverse" if max_buckl > 1.0 else "normal")

    # Combination Case
    if ':' in combo_val:
        parts = combo_val.split(':', 1)
        c_title = parts[0].strip()
        c_desc = parts[1].strip()
        st.markdown(f"**{c_title}**")

        sub_parts = [p.strip() for p in c_desc.split(':') if p.strip()]
        if sub_parts:
            c_cols = st.columns(2)
            for i, sp in enumerate(sub_parts):
                with c_cols[i % 2]:
                    st.markdown(f"**{i + 1}.** {sp}")
        else:
            st.markdown(f"{c_desc}")
    else:
        st.markdown(f"**{combo_val}**")

    st.divider()

    # --- SECTION 1: FORCE DIAGRAMS ---
    st.header("Force Diagrams")
    if data['forces']:
        df = pd.DataFrame(data['forces'], columns=['Pos', 'Fx', 'Fy', 'Fz', 'Mxx', 'Myy', 'Mzz'])
        df = df.sort_values('Pos')

        c1, c2, c3 = st.columns(3)

        # Get dynamic units or default to kN/kNm
        u_fx = data['units'].get('Fx', 'kN')
        u_fy = data['units'].get('Fy', 'kN')
        u_fz = data['units'].get('Fz', 'kN')
        u_mx = data['units'].get('Mxx', 'kNm')
        u_my = data['units'].get('Myy', 'kNm')
        u_mz = data['units'].get('Mzz', 'kNm')

        with c1:
            st.subheader(f"Axial Force ({u_fx})")
            chart = plot_with_zero_line(df, ['Fx'], ['#0000FF'], f"Force ({u_fx})")
            st.altair_chart(chart, use_container_width=True)

        with c2:
            st.subheader(f"Shear Forces ({u_fy})")
            chart = plot_with_zero_line(df, ['Fy', 'Fz'], ['#00FF00', '#FF0000'], f"Force ({u_fy})")
            st.altair_chart(chart, use_container_width=True)

        with c3:
            st.subheader(f"Moments ({u_mx})")
            chart = plot_with_zero_line(df, ['Mxx', 'Myy', 'Mzz'], ['#00FFFF', '#FF00FF', '#FFFF00'],
                                        f"Moment ({u_mx})")
            st.altair_chart(chart, use_container_width=True)

    else:
        st.info("No detailed force table found. (This is common for 'Brief' design outputs).")

    # --- SECTION 2: INPUT DATA ---
    st.header("Design Parameters")
    with st.expander("Show Design Parameters", expanded=True):
        all_lines = data['input_data'] + data['effective_lengths']

        if all_lines:
            cols = st.columns(3)
            chunk_size = (len(all_lines) + 2) // 3

            for i in range(3):
                with cols[i]:
                    start = i * chunk_size
                    end = start + chunk_size
                    subset = all_lines[start:end]

                    html_content = ""
                    for line in subset:
                        tabs = line.count('\t')
                        text = line.strip()
                        if not text: continue

                        is_bold = tabs <= 2
                        visual_indent_level = max(0, tabs - 1)
                        indent_px = visual_indent_level * 15

                        weight = "bold" if is_bold else "normal"
                        style = f"padding-left: {indent_px}px; margin-bottom: 2px; font-weight: {weight}; font-size: 0.9rem;"
                        html_content += f"<div style='{style}'>{text}</div>"

                    st.markdown(html_content, unsafe_allow_html=True)
        else:
            st.info("No design parameters found.")

    # --- SECTION 3: LOCAL CHECKS ---
    st.header("Local Capacity Checks")
    if data['local_checks']:
        unique_checks = {}
        for check in data['local_checks']:
            name = check['name']
            if name not in unique_checks or check['util'] > unique_checks[name]['util']:
                unique_checks[name] = check

        final_checks = list(unique_checks.values())

        cols = st.columns(2)
        for i, check in enumerate(final_checks):
            util = check['util']
            icon = "🟢" if util <= 1.0 else "🔴"
            name = check['name'].strip(':')

            left_text = f"**{name}**"
            right_text = f"(Util: {util:.3f}) {icon}"

            width = 36
            label_text = f"{left_text:<{width // 2}}{right_text:>{width // 2}}"
            label = label_text.replace(" ", "\u00A0")

            with cols[i % 2]:
                with st.expander(label, expanded=expand_all):
                    if check.get('split_data'):
                        sd = check['split_data']
                        # Render Header (skip first line which is title)
                        render_check_lines(sd['header'][1:], -1)

                        c_lh, c_rh = st.columns(2)
                        with c_lh:
                            st.markdown("**Left Hand End Result**")
                            render_check_lines(sd['lh']['lines'], sd['lh']['util'])
                        with c_rh:
                            st.markdown("**Right Hand End Result**")
                            render_check_lines(sd['rh']['lines'], sd['rh']['util'])
                    else:
                        render_check_lines(check['lines'][1:], util)
    else:
        st.info("No local capacity checks found.")

    # --- SECTION 4: BUCKLING CHECKS ---
    st.header("Buckling Capacity Checks")
    if data['buckling_checks']:
        cols = st.columns(2)
        for i, check in enumerate(data['buckling_checks']):
            util = check['util']
            icon = "🟢" if util <= 1.0 else "🔴"
            name = check['name'].strip(':')

            left_text = f"**{name}**"
            right_text = f"(Util: {util:.3f}) {icon}"
            width = 36
            label_text = f"{left_text:<{width // 2}}{right_text:>{width // 2}}"
            label = label_text.replace(" ", "\u00A0")

            with cols[i % 2]:
                with st.expander(label, expanded=expand_all):
                    render_check_lines(check['lines'][1:], util)
    else:
        st.info("No buckling capacity checks found.")
