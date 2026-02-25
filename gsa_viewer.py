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
        'buckling_checks': [],
        'summary_data': {  # specific fields for the summary section
            'section_desc': None,
            'section_props': [],
            'steel_grade': None,
            'buckling_class': None
        }
    }

    # --- Parsing Flags ---
    section = None  # 'INPUT', 'EFFECTIVE', 'LOCAL', 'BUCKLING'
    skip_input_block = False

    # Keywords to identify specific checks
    target_local = [
        "Axial tension check", "Axial compression check",
        "Major axis bending check", "Minor axis bending check", "Torsion check",
        "Major axis shear check", "Minor axis shear check",
        "Combined biaxial bending and tension check", "Combined biaxial bending and compression check"
    ]
    target_buckling = [
        "Check axial buckling major axis", "Check axial buckling minor axis",
        "Check LT buckling", "Check FT buckling"
    ]

    current_check = None
    current_perm = "N/A"  # Track permutation context statefully

    # State for capturing multi-line input data fields
    capture_section_props = False
    section_props_indent = -1

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

        # FIX: Remove CSV artifact quotes (e.g. "Text" -> Text)
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

            if "Forces, Moments" in clean_content:  # Section Data handled below
                pass
            elif clean_content:
                data['input_data'].append(display_line)

                # --- Specific Field Extraction for Summary ---
                # 1. Section Description
                if "Section description" in clean_content:
                    parts = clean_content.split(":", 1)
                    if len(parts) > 1 and parts[1].strip():
                        data['summary_data']['section_desc'] = parts[1].strip()
                    else:
                        try:
                            next_line = lines[i + 1]
                            next_commas = 0
                            for c in next_line:
                                if c == ',':
                                    next_commas += 1
                                else:
                                    break
                            if next_commas > leading_commas:
                                val = next_line[next_commas:].strip().rstrip(',').strip('"')
                                data['summary_data']['section_desc'] = val
                        except:
                            pass

                # 2. Steel Grade
                if "Steel grade" in clean_content or "Material:" in clean_content:
                    parts = clean_content.split(":", 1)
                    if len(parts) > 1 and parts[1].strip():
                        data['summary_data']['steel_grade'] = parts[1].strip()
                    else:
                        try:
                            next_line = lines[i + 1]
                            next_commas = 0
                            for c in next_line:
                                if c == ',':
                                    next_commas += 1
                                else:
                                    break
                            if next_commas > leading_commas:
                                val = next_line[next_commas:].strip().rstrip(',').strip('"')
                                data['summary_data']['steel_grade'] = val
                        except:
                            pass

                # 3. Local Buckling Classification
                if "Compression" in clean_content and "buckling classification" in clean_content.lower():
                    # Extract text after the last colon (e.g. "Compression: Compact" -> "Compact")
                    parts = clean_content.split(":")
                    if len(parts) > 1:
                        data['summary_data']['buckling_class'] = parts[-1].strip()
                elif "Compression" in clean_content and "classification" in clean_content.lower():
                    # Catch simpler variants
                    parts = clean_content.split(":")
                    if len(parts) > 1:
                        data['summary_data']['buckling_class'] = parts[-1].strip()

                # 4. Section Properties
                if "Section Properties" in clean_content or "Section Data" in clean_content:
                    capture_section_props = True
                    section_props_indent = leading_commas
                elif capture_section_props:
                    if leading_commas <= section_props_indent:
                        capture_section_props = False
                    elif "Section description" in clean_content:
                        capture_section_props = False
                    else:
                        if clean_content:
                            data['summary_data']['section_props'].append(clean_content)

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
                    for k, v in enumerate(parts[1:7]):
                        match = re.search(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", v)
                        if match:
                            num_str = match.group()
                            try:
                                val = float(num_str)
                            except:
                                val = 0.0
                            vals.append(val)
                            if not units_detected and k < len(unit_keys):
                                unit_str = v.replace(num_str, '').strip()
                                if unit_str:
                                    units[unit_keys[k]] = unit_str
                        else:
                            vals.append(0.0)
                    if vals:
                        units_detected = True
                    forces.append([pos] + vals)
    return forces, units


def extract_float(text):
    match = re.search(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", text)
    if match:
        try:
            return float(match.group())
        except:
            return 0.0
    return None


def find_util(lines):
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
            'is_split': True, 'header': header,
            'lh': {'lines': lh_lines, 'util': find_util(lh_lines)},
            'rh': {'lines': rh_lines, 'util': find_util(rh_lines)}
        }
    return {'is_split': False, 'lines': lines, 'util': find_util(lines)}


# ==========================================
# 2. STREAMLIT UI HELPER
# ==========================================
def render_check_lines(lines, util_val, check_low_util=True, columns=1):
    if not lines:
        return

    # Check for "No axial compression" dominance first
    no_compression_found = False
    for line in lines:
        if "No axial compression" in line:
            no_compression_found = True
            break

    if no_compression_found:
        st.markdown(
            "<div style='padding-left: 0px; font-size: 0.9rem; margin-bottom: 2px;'>No axial compression in this span.</div>",
            unsafe_allow_html=True
        )
        return

    # Check for low utilization
    if check_low_util and util_val < 0.001:
        st.markdown(
            "<div style='padding: 4px 8px; font-size: 0.9rem; margin-bottom: 2px; border-radius: 4px;'>Utilization lower than 0.001, not governing.</div>",
            unsafe_allow_html=True
        )
        return

    valid_lines = [l for l in lines if l.strip()]
    min_tabs = min([l.count('\t') for l in valid_lines]) if valid_lines else 0

    rendered_html = []

    for line in lines:
        clean_text = line.strip()
        if not clean_text: continue

        # Replacements
        clean_text = clean_text.replace('phi', 'Φ')
        clean_text = clean_text.replace('*', '×')

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
                        style_props = "background-color: #d4edda; color: #155724;"
                        if v > 1.0:
                            style_props += "background-color: #f8d7da; color: #721c24;"
                        elif v > 0.9:
                            style_props += "background-color: #fff3cd; color: #856404;"
                        return f"<span style='{style_props}'>{m.group()}</span>"
                except:
                    pass
                return m.group()

            processed_text = re.sub(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", repl, clean_text)

            # Highlight AISC References (Green background, dark green text)
            def ref_repl(m):
                return f"<span style='background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; border-radius: 3px; padding: 0 4px; font-weight: 500;'>{m.group(0)}</span>"

            processed_text = re.sub(r"ref\.?\s*AISC.*$", ref_repl, processed_text, flags=re.IGNORECASE)

        html_line = f"<div style='padding-left: {indent_px}px; font-size: 0.9rem; margin-bottom: 2px;'>{processed_text}</div>"
        rendered_html.append(html_line)

    if columns > 1 and rendered_html:
        cols = st.columns(columns)
        chunk_size = (len(rendered_html) + columns - 1) // columns
        for i in range(columns):
            with cols[i]:
                subset = rendered_html[i * chunk_size: (i + 1) * chunk_size]
                combined_html = "".join(subset)
                st.markdown(combined_html, unsafe_allow_html=True)
    else:
        for html in rendered_html:
            st.markdown(html, unsafe_allow_html=True)


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
        expand_all = st.checkbox("Expand all details (for printing)", value=False)

    # WRAP REPORT CONTENT IN CONTAINER FOR SCREENSHOT
    with st.container():
        st.markdown('<span id="report_start_marker"></span>', unsafe_allow_html=True)

        # ---------------------------------------------------------------------
        # PART 1: SUMMARY, FORCES, DESIGN PARAMETERS
        # ---------------------------------------------------------------------
        with st.container():
            st.markdown('<span id="part1_root"></span>', unsafe_allow_html=True)

            # --- DASHBOARD SUMMARY ---
            total_local = len(data['local_checks'])
            total_buckling = len(data['buckling_checks'])

            max_local = max([c['util'] for c in data['local_checks']]) if total_local > 0 else 0.0
            max_buckl = max([c['util'] for c in data['buckling_checks']]) if total_buckling > 0 else 0.0

            member_val = data['metadata'].get('member', 'N/A')
            combo_val = data['metadata'].get('combo', 'Combination Case: N/A')

            # Top Row Metrics CSS
            st.markdown("""
                <style>
                [data-testid="stMetric"] { text-align: center; margin: auto; }
                [data-testid="stMetricLabel"] { justify-content: center; }
                [data-testid="stMetricValue"] { justify-content: center; }
                </style>
            """, unsafe_allow_html=True)

            col_sum1, col_sum2, col_sum3 = st.columns(3)
            col_sum1.metric("Member", member_val, border=True)
            col_sum2.metric("Max Local Util", f"{max_local:.1%}",
                            delta_color="inverse" if max_local > 1.0 else "normal", border=True)
            col_sum3.metric("Max Buckling Util", f"{max_buckl:.1%}",
                            delta_color="inverse" if max_buckl > 1.0 else "normal", border=True)

            # --- SECTION 1: FORCE DIAGRAMS ---
            if data['forces']:
                df = pd.DataFrame(data['forces'], columns=['Pos', 'Fx', 'Fy', 'Fz', 'Mxx', 'Myy', 'Mzz'])
                df = df.sort_values('Pos')
                c1, c2, c3 = st.columns(3)
                u_fx = data['units'].get('Fx', 'kN')
                u_fy = data['units'].get('Fy', 'kN')
                u_mx = data['units'].get('Mxx', 'kNm')

                with c1:
                    chart = plot_with_zero_line(df, ['Fx'], ['#0000FF'], f"Force ({u_fx})")
                    st.altair_chart(chart, use_container_width=True, theme=None)
                with c2:
                    chart = plot_with_zero_line(df, ['Fy', 'Fz'], ['#00FF00', '#FF00FF'], f"Force ({u_fy})")
                    st.altair_chart(chart, use_container_width=True, theme=None)
                with c3:
                    chart = plot_with_zero_line(df, ['Mxx', 'Myy', 'Mzz'], ['#00FFFF', '#FF00FF', '#FFFF00'],
                                                f"Moment ({u_mx})")
                    st.altair_chart(chart, use_container_width=True, theme=None)
            else:
                st.info("No detailed force table found. (This is common for 'Brief' design outputs).")

            # --- SECTION 2: DESIGN PARAMETERS ---
            summ = data['summary_data']
            raw_desc = summ.get('section_desc', 'Design Parameters')
            section_title = raw_desc
            if section_title:
                if section_title.strip().upper().startswith("CAT"):
                    section_title = section_title.strip()[3:].strip()
            if not section_title or section_title == 'Not found':
                section_title = "Design Parameters"

            st.subheader('Section: ' + section_title)
            st.markdown("*design wall thickness = 0.93 × nominal wall thickness*")

            props_dict = {}
            dims_found = []
            g1_found = []

            if summ['section_props']:
                for line in summ['section_props']:
                    clean = line.strip()
                    if '=' in clean:
                        parts = clean.split('=', 1)
                        k = parts[0].strip()
                        v = parts[1].strip()
                        props_dict[k] = v

                dim_keys = ['D', 'B', 'B1', 'B2', 'Tf', 'Tw', 'T1', 'T2', 'Ro', 'R', 'Ri', 'OD', 'ID', 't', 'd', 'b',
                            'dia', 'Diameter', 'Depth', 'Width']
                for k in dim_keys:
                    if k in props_dict: dims_found.append(f"{k} = {props_dict[k]}")

                g1_keys = ['A', 'Ixx', 'Iyy', 'J', 'Sxx', 'Syy', 'Zxx', 'Zyy', 'xbar', 'ybar']
                for k in g1_keys:
                    if k in props_dict: g1_found.append(f"{k} = {props_dict[k]}")

            header_parts = []
            if dims_found:
                header_parts.append(f"**Section Dimensions:** {', '.join(dims_found)}")
            grade = summ.get('steel_grade', 'Not found')
            header_parts.append(f"**Steel Grade:** {grade}")
            buckling_class = summ.get('buckling_class')
            if buckling_class:
                header_parts.append(f"**{buckling_class}**")

            st.markdown(", ".join(header_parts))

            if g1_found:
                st.markdown(f"**Section Properties:** {', '.join(g1_found)}", unsafe_allow_html=True)
            elif summ['section_props'] and not dims_found:
                prop_str = "<br>".join([p.strip() for p in summ['section_props']])
                st.markdown(f"**Section Properties:** {prop_str}", unsafe_allow_html=True)
            else:
                if not dims_found and not summ['section_props']:
                    st.markdown("**Section Properties:** _Not found_")

            if data['effective_lengths']:
                eff_vals = {'Lxx': None, 'Lyy': None, 'Llt': None}
                for axis in ['Lxx', 'Lyy', 'Llt']:
                    for line in data['effective_lengths']:
                        clean = line.replace('(', ' ').replace(')', ' ').replace(',', ' ')
                        match = re.search(rf"\b{axis}\s*=\s*([^\s]+)", clean, re.IGNORECASE)
                        if match:
                            eff_vals[axis] = match.group(1)
                            break
                display_parts = []
                if eff_vals['Lxx']: display_parts.append(f"Lxx = {eff_vals['Lxx']}")
                if eff_vals['Lyy']: display_parts.append(f"Lyy = {eff_vals['Lyy']}")
                if eff_vals['Llt']: display_parts.append(f"Llt = {eff_vals['Llt']}")

                if display_parts:
                    st.markdown(f"**Effective Lengths:** {', '.join(display_parts)}")
                else:
                    st.markdown("**Effective Lengths:**")
                    html_content = ""
                    for line in data['effective_lengths']:
                        clean = line.strip()
                        if not clean: continue
                        tabs = line.count('\t')
                        indent = tabs * 15
                        html_content += f"<div style='margin-left: {indent}px; font-size: 0.9em;'>{clean}</div>"
                    st.markdown(html_content, unsafe_allow_html=True)
            else:
                st.markdown("**Effective Lengths:** _Not found_")

        # ---------------------------------------------------------------------
        # PART 2: LOCAL CHECKS
        # ---------------------------------------------------------------------
        with st.container():
            st.markdown('<span id="part2_root"></span>', unsafe_allow_html=True)
            st.subheader("Local Capacity Checks")
            if data['local_checks']:
                unique_checks = {}
                for check in data['local_checks']:
                    name = check['name']
                    if name not in unique_checks or check['util'] > unique_checks[name]['util']:
                        unique_checks[name] = check
                final_checks = list(unique_checks.values())

                grid_order = [
                    "Axial tension check", "Axial compression check",
                    "Major axis bending check", "Minor axis bending check", "Torsion check",
                    "Major axis shear check", "Minor axis shear check"
                ]
                combined_names = [
                    "Combined biaxial bending and tension check",
                    "Combined biaxial bending and compression check"
                ]
                full_order_list = grid_order + combined_names
                order_map = {name: i for i, name in enumerate(full_order_list)}

                high_util_checks = [c for c in final_checks if c['util'] >= 0.001]
                low_util_checks = [c for c in final_checks if c['util'] < 0.001]

                if high_util_checks:
                    high_grid = [c for c in high_util_checks if c['name'] not in combined_names]
                    high_full = [c for c in high_util_checks if c['name'] in combined_names]
                    high_grid.sort(key=lambda x: order_map.get(x['name'], 999))

                    for check in high_full:
                        util = check['util']
                        icon = "🟢" if util <= 1.0 else "🔴"
                        name = check['name'].strip(':')
                        label = f"**{name}** \u00A0\u00A0 (Util: {util:.3f}) {icon}"
                        with st.expander(label, expanded=expand_all):
                            if check.get('split_data'):
                                sd = check['split_data']
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

                    if high_grid:
                        cols = st.columns(3)
                        for i, check in enumerate(high_grid):
                            util = check['util']
                            icon = "🟢" if util <= 1.0 else "🔴"
                            name = check['name'].strip(':')
                            label = f"**{name}** \u00A0\u00A0 (Util: {util:.3f}) {icon}"
                            with cols[i % 3]:
                                with st.expander(label, expanded=expand_all):
                                    if check.get('split_data'):
                                        sd = check['split_data']
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

                if low_util_checks:
                    if high_util_checks:
                        st.markdown("**Non-Governing Checks (Util < 0.001)**")
                    low_util_checks.sort(key=lambda x: order_map.get(x['name'], 999))
                    cols = st.columns(3)
                    for i, check in enumerate(low_util_checks):
                        util = check['util']
                        icon = "🟢"
                        name = check['name'].strip(':')
                        label = f"**{name}** \u00A0\u00A0 (Util: {util:.3f}) {icon}"
                        with cols[i % 3]:
                            with st.expander(label, expanded=False):
                                render_check_lines(check['lines'][1:], util)
            else:
                st.info("No local capacity checks found.")

        # ---------------------------------------------------------------------
        # PART 3: BUCKLING CHECKS
        # ---------------------------------------------------------------------
        with st.container():
            st.markdown('<span id="part3_root"></span>', unsafe_allow_html=True)
            st.subheader("Buckling Capacity")
            if data['buckling_checks']:
                high_util_buckling = [c for c in data['buckling_checks'] if c['util'] >= 0.001]
                low_util_buckling = [c for c in data['buckling_checks'] if c['util'] < 0.001]

                if high_util_buckling:
                    for i, check in enumerate(high_util_buckling):
                        util = check['util']
                        icon = "🟢" if util <= 1.0 else "🔴"
                        name = check['name'].strip(':')
                        label = f"**{name}** \u00A0\u00A0 (Util: {util:.3f}) {icon}"
                        with st.expander(label, expanded=expand_all):
                            render_check_lines(check['lines'][1:], util, check_low_util=False, columns=3)

                if low_util_buckling:
                    if high_util_buckling:
                        st.markdown("**Non-Governing Checks (Util < 0.001)**")
                    cols = st.columns(3)
                    for i, check in enumerate(low_util_buckling):
                        util = check['util']
                        icon = "🟢"
                        name = check['name'].strip(':')
                        label = f"**{name}** \u00A0\u00A0 (Util: {util:.3f}) {icon}"
                        with cols[i % 3]:
                            with st.expander(label, expanded=False):
                                render_check_lines(check['lines'][1:], util, check_low_util=False)
            else:
                st.info("No buckling capacity checks found.")


    def render_export_options():
        js = """
        <script src="https://cdnjs.cloudflare.com/ajax/libs/html-to-image/1.11.11/html-to-image.min.js"></script>
        <script>
        function getNode(markerId) {
            var marker = parent.document.getElementById(markerId);
            if (marker) {
                // Return the closest Streamlit vertical block wrapper
                // This works for individual parts AND the full report wrapper
                return marker.closest('[data-testid="stVerticalBlock"]');
            }
            return null;
        }

        var options = {
            pixelRatio: 3, 
            backgroundColor: 'white'
        };

        function copyToClipboard(markerId, btnId) {
            var node = getNode(markerId);
            var btn = document.getElementById(btnId);
            var originalHTML = btn.innerHTML;

            if (!node) {
                btn.innerHTML = '<span class="btn-icon">❌</span> Not Found';
                setTimeout(function() { btn.innerHTML = originalHTML; }, 2000);
                return;
            }

            btn.innerHTML = '<span class="btn-icon">⏳</span> Processing...';

            htmlToImage.toBlob(node, options)
                .then(function (blob) {
                    navigator.clipboard.write([
                        new ClipboardItem({ 'image/png': blob })
                    ]).then(function () {
                        btn.innerHTML = '<span class="btn-icon">✅</span> Copied!';
                        setTimeout(function() { btn.innerHTML = originalHTML; }, 2000);
                    }).catch(function (error) {
                        console.error('Clipboard write error:', error);
                        btn.innerHTML = '<span class="btn-icon">❌</span> Clipboard Error';
                        setTimeout(function() { btn.innerHTML = originalHTML; }, 2000);
                    });
                })
                .catch(function (error) {
                    console.error('Generation error:', error);
                    btn.innerHTML = '<span class="btn-icon">❌</span> Render Error';
                    setTimeout(function() { btn.innerHTML = originalHTML; }, 2000);
                });
        }
        </script>

        <style>
            .copy-btn {
                background-color: #2b88d8;
                color: white;
                border: none;
                padding: 10px 15px;
                border-radius: 5px;
                cursor: pointer;
                font-weight: bold;
                width: 100%;
                text-align: left;
                margin-bottom: 8px;
                display: flex;
                align-items: center;
                font-family: "Source Sans Pro", sans-serif;
            }
            .copy-btn:hover {
                background-color: #0078D4;
            }
            .copy-btn-primary {
                background-color: #28a745;
            }
            .copy-btn-primary:hover {
                background-color: #218838;
            }
            .btn-icon {
                margin-right: 10px;
                font-size: 1.2em;
            }
            .row-container {
                display: flex;
                flex-direction: column;
                gap: 5px;
                margin-top: 10px;
            }
            .divider {
                height: 1px;
                background-color: #e6e6e6;
                margin: 10px 0;
            }
        </style>

        <div class="row-container">
            <button id="btn_full" class="copy-btn copy-btn-primary" onclick="copyToClipboard('report_start_marker', 'btn_full')">
                <span class="btn-icon">📸</span> Copy Full Report (All Parts Merged)
            </button>

            <div class="divider"></div>

            <button id="btn1" class="copy-btn" onclick="copyToClipboard('part1_root', 'btn1')">
                <span class="btn-icon">📋</span> Copy Part 1: General Info, Forces & Params
            </button>
            <button id="btn2" class="copy-btn" onclick="copyToClipboard('part2_root', 'btn2')">
                <span class="btn-icon">📋</span> Copy Part 2: Local Capacity Checks
            </button>
            <button id="btn3" class="copy-btn" onclick="copyToClipboard('part3_root', 'btn3')">
                <span class="btn-icon">📋</span> Copy Part 3: Buckling Capacity Checks
            </button>
        </div>
        """
        components.html(js, height=240)


    render_export_options()
