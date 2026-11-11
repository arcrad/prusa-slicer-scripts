import sys
import os
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FATAL_LOG_PATH = os.path.join(SCRIPT_DIR, "prusa_fatal_error.txt")

try:
    import re
    import math
    from datetime import datetime

    try:
        import tkinter as tk
        HAS_TKINTER = True
    except ImportError:
        HAS_TKINTER = False

    # --- CONFIGURATION ---
    AREA_THRESHOLD_MM2 = 150.0       # Minimum true air bridge area (mm²)
    MIN_CONTINUOUS_VOID_MM = 30.0   # Must span > 30mm continuously over ZERO underlying lines
    GRID_RES_MM = 0.8                # Spatial resolution for layer grid mapping (mm)
    
    TEMP_DROP_C = 15                 # Temp drop during ironing to prevent wood charring/oozing
    IRONING_FEEDRATE = 1300          # Speed of ironing passes (mm/min) -> 30mm/s
    Z_HOP_HEIGHT = 2.0               # Safe clearance height (mm)
    UI_TIMEOUT_SEC = 60              # Auto-accept timeout
    # ---------------------

    def get_xy(line, current_x, current_y):
        x_match = re.search(r'\bX([-\d.]+)', line)
        y_match = re.search(r'\bY([-\d.]+)', line)
        new_x = float(x_match.group(1)) if x_match else current_x
        new_y = float(y_match.group(1)) if y_match else current_y
        return new_x, new_y

    def is_printing_move(line):
        if not (line.startswith('G1') or line.startswith('G2') or line.startswith('G3')):
            return False
        if 'X' not in line and 'Y' not in line:
            return False
        e_match = re.search(r'\bE([-\d.]+)', line)
        if e_match:
            try:
                if float(e_match.group(1)) > 0:
                    return True
            except ValueError:
                pass
        return False

    def rasterize_line(x1, y1, x2, y2, grid_res):
        dist = math.hypot(x2 - x1, y2 - y1)
        if dist == 0:
            return [(int(x1 / grid_res), int(y1 / grid_res))]
        steps = max(1, int(dist / (grid_res / 2.0)))
        cells = set()
        for i in range(steps + 1):
            t = i / steps
            px = x1 + t * (x2 - x1)
            py = y1 + t * (y2 - y1)
            cells.add((int(px / grid_res), int(py / grid_res)))
        return cells

    def show_interactive_dialog(summary_text, timeout_sec, log_func):
        if not HAS_TKINTER:
            log_func("  -> Tkinter UI library not found on this Python install. Auto-accepting modifications.")
            return True
            
        root = tk.Tk()
        root.title("PrusaSlicer True Bridge Optimizer")
        root.attributes("-topmost", True)
        root.geometry("480x280")
        
        try:
            root.eval('tk::PlaceWindow . center')
        except:
            pass
        
        result = {'proceed': True}
        
        lbl = tk.Label(root, text=summary_text, justify="left", font=("Segoe UI", 10), wraplength=440)
        lbl.pack(padx=20, pady=20, expand=True, fill="both")
        
        time_left = tk.IntVar(value=timeout_sec)
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=15)
        
        def on_yes():
            result['proceed'] = True
            root.destroy()
            
        def on_no():
            result['proceed'] = False
            root.destroy()
            
        btn_yes = tk.Button(btn_frame, text=f"Proceed ({time_left.get()}s)", command=on_yes, width=15, bg="#4CAF50", fg="white", font=("Segoe UI", 10, "bold"))
        btn_yes.pack(side="left", padx=10)
        
        btn_no = tk.Button(btn_frame, text="Cancel Export", command=on_no, width=15, font=("Segoe UI", 10))
        btn_no.pack(side="right", padx=10)
        
        def update_timer():
            t = time_left.get() - 1
            if t <= 0:
                on_yes()
            else:
                time_left.set(t)
                btn_yes.config(text=f"Proceed ({t}s)")
                root.after(1000, update_timer)
                
        root.after(1000, update_timer)
        root.mainloop()
        return result['proceed']

    class Block:
        def __init__(self, category, start_x, start_y):
            self.category = category
            self.lines = []
            self.start_x = start_x
            self.start_y = start_y
            self.end_x = start_x
            self.end_y = start_y
            self.bridge_toolpath = []
            self.in_bridge = False

        def add_line(self, line, current_x, current_y):
            self.lines.append(line)
            self.end_x = current_x
            self.end_y = current_y
            
            if line.startswith(';TYPE:'):
                self.in_bridge = (line.strip() == ';TYPE:Bridge infill')
            
            if self.in_bridge:
                if is_printing_move(line):
                    self.bridge_toolpath.append(line)
                elif 'X' in line or 'Y' in line:
                    self.bridge_toolpath.append(line)

    def generate_ironing(bridge_lines, t_data, retract_amt, unretract_amt, start_x, start_y):
        iron_moves = []
        for b_line in bridge_lines:
            if is_printing_move(b_line):
                b_line = re.sub(r'\sE[-\d.]+', '', b_line)
                if 'F' in b_line:
                    b_line = re.sub(r'F\d+', f'F{IRONING_FEEDRATE}', b_line)
                else:
                    b_line = b_line.strip() + f" F{IRONING_FEEDRATE}\n"
                iron_moves.append(b_line)
        
        if not iron_moves:
            return [], start_x, start_y
            
        z_cur = t_data['z']
        z_knockdown = z_cur + (t_data['lh'] * 2)
        temp_norm = t_data['temp']
        temp_iron = temp_norm - TEMP_DROP_C
        
        bridge_start_x, bridge_start_y = start_x, start_y
        for b_line in bridge_lines:
            bx, by = get_xy(b_line, None, None)
            if bx is not None and by is not None:
                bridge_start_x, bridge_start_y = bx, by
                break
        
        seq = [
            f"; --- AUTO-BRIDGE IRONING START ---\n",
            f"M104 S{temp_iron} ; Drop temp to prevent charring\n",
            f"G1 E{retract_amt} F2400 ; Retract\n",
            f"G0 Z{z_cur + Z_HOP_HEIGHT:.3f} F7200 ; Safe Z-Hop\n",
            f"G0 X{bridge_start_x:.3f} Y{bridge_start_y:.3f} F7200 ; Travel to bridge start\n",
            f"G0 Z{z_knockdown:.3f} F7200 ; PASS 1: Knockdown (Z+{t_data['lh']*2:.3f})\n"
        ]
        seq.extend(iron_moves)
        
        end_x, end_y = bridge_start_x, bridge_start_y
        for im in iron_moves:
            end_x, end_y = get_xy(im, end_x, end_y)
            
        seq.extend([
            f"G0 Z{z_cur + Z_HOP_HEIGHT:.3f} F7200 ; Safe Z-Hop\n",
            f"G0 X{bridge_start_x:.3f} Y{bridge_start_y:.3f} F7200 ; Return to bridge start\n",
            f"G0 Z{z_cur:.3f} F7200 ; PASS 2: Flush Ironing\n"
        ])
        seq.extend(iron_moves)
        
        seq.extend([
            f"G0 Z{z_cur + Z_HOP_HEIGHT:.3f} F7200 ; Safe Z-Hop\n",
            f"M104 S{temp_norm} ; Restore temp\n",
            f"G0 X{end_x:.3f} Y{end_y:.3f} F7200 ; Ensure nozzle at end of ironing\n",
            f"G1 E{unretract_amt} F2400 ; Unretract\n",
            f"G0 Z{z_cur:.3f} F7200 ; Restore Z for next layer\n",
            f"; --- AUTO-BRIDGE IRONING END ---\n"
        ])
        
        return seq, end_x, end_y

    def process_target_layer(lines, start_x, start_y, layer_data):
        blocks = []
        curr_x, curr_y = start_x, start_y
        
        retract_amt, unretract_amt = -0.8, 0.8
        for line in lines:
            match = re.search(r'\bE(-[\d.]+)', line)
            if match:
                val = float(match.group(1))
                if val < -0.15:
                    retract_amt, unretract_amt = val, abs(val)
                    break
        
        current_cat = 'other'
        current_block = Block('other', curr_x, curr_y)
        
        for line in lines:
            if line.startswith(';TYPE:'):
                type_str = line.strip()
                if type_str in [';TYPE:Perimeter', ';TYPE:External perimeter', ';TYPE:Overhang perimeter']:
                    new_cat = 'perimeter'
                elif type_str in [';TYPE:Infill', ';TYPE:Solid infill', ';TYPE:Internal infill', ';TYPE:Bridge infill', ';TYPE:Top solid infill', ';TYPE:Bottom solid infill']:
                    new_cat = 'infill'
                else:
                    new_cat = 'other'
                    
                if new_cat != current_cat:
                    blocks.append(current_block)
                    current_block = Block(new_cat, curr_x, curr_y)
                    current_cat = new_cat
            
            if is_printing_move(line) or 'X' in line or 'Y' in line:
                curr_x, curr_y = get_xy(line, curr_x, curr_y)
            
            current_block.add_line(line, curr_x, curr_y)
            
        blocks.append(current_block)
        
        # Safely swap adjacent Perimeter and Infill blocks, leaving everything else untouched
        i = 0
        while i < len(blocks) - 1:
            if blocks[i].category == 'perimeter' and blocks[i+1].category == 'infill':
                blocks[i], blocks[i+1] = blocks[i+1], blocks[i]
                i += 2
            else:
                i += 1
        
        layer_out = []
        printer_x, printer_y = start_x, start_y
        
        for b in blocks:
            if not b.lines:
                continue
                
            # If the block expects the nozzle somewhere else, inject travel to exactly where it should be
            if (printer_x is not None and b.start_x is not None and 
               (abs(printer_x - b.start_x) > 0.005 or abs(printer_y - b.start_y) > 0.005)):
                layer_out.extend([
                    f"G1 E{retract_amt} F2400 ; INJECTED RETRACT\n",
                    f"G0 X{b.start_x:.3f} Y{b.start_y:.3f} F7200 ; RESTORE ORIG POSITION FOR {b.category.upper()}\n",
                    f"G1 E{unretract_amt} F2400 ; INJECTED UNRETRACT\n"
                ])
                printer_x, printer_y = b.start_x, b.start_y
            
            layer_out.extend(b.lines)
            printer_x, printer_y = b.end_x, b.end_y
            
            if b.bridge_toolpath:
                iron_seq, ix, iy = generate_ironing(b.bridge_toolpath, layer_data, retract_amt, unretract_amt, printer_x, printer_y)
                layer_out.extend(iron_seq)
                printer_x, printer_y = ix, iy
                
        return layer_out, printer_x, printer_y

    def process_gcode(file_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(SCRIPT_DIR, f"prusa_swap_log_{timestamp}.txt")
        
        def log(msg):
            with open(log_path, 'a', encoding='utf-8') as log_file:
                log_file.write(msg + '\n')

        log("--- STARTING ADVANCED TRUE-BRIDGE OPTIMIZER ---")
        
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        # ==========================================
        # PASS 1: CONTINUOUS VOID SPAN ANALYSIS
        # ==========================================
        log("\n[PASS 1] Evaluating continuous open-air span distances...")
        
        target_layers = {}
        layer_printed_grids = {}
        
        current_layer = 0
        current_z = 0.0
        prev_z = 0.0
        
        max_total_time = 0
        current_remaining_mins = 0
        current_temp = 220
        
        in_bridge_block = False
        last_x, last_y = 0.0, 0.0
        layer_air_bridge_area = 0.0

        def format_elapsed_time():
            if max_total_time == 0:
                return "Unknown"
            elapsed = max(0, max_total_time - current_remaining_mins)
            hrs = elapsed // 60
            mins = elapsed % 60
            return f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"

        for line in lines:
            if line.startswith('M73 P') and 'R' in line:
                match = re.search(r'R(\d+)', line)
                if match:
                    r_val = int(match.group(1))
                    max_total_time = max(max_total_time, r_val)
                    current_remaining_mins = r_val
                        
            if line.startswith('M104 S'):
                match = re.search(r'S(\d+)', line)
                if match:
                    current_temp = int(match.group(1))

            if line.startswith(';Z:'):
                prev_z = current_z
                current_z = float(line.strip().split(':')[1])

            if line.startswith('; LAYER_CHANGE') or line.startswith(';LAYER_CHANGE'):
                if layer_air_bridge_area >= AREA_THRESHOLD_MM2:
                    time_str = format_elapsed_time()
                    log(f"  -> Target Confirmed on Layer {current_layer}: True Open-Air Area = {layer_air_bridge_area:.1f}mm² (at ~{time_str})")
                    target_layers[current_layer] = {
                        'z': current_z,
                        'lh': max(0.05, current_z - prev_z),
                        'temp': current_temp,
                        'areas': [layer_air_bridge_area],
                        'time_est': time_str
                    }
                elif layer_air_bridge_area > 0:
                    log(f"  -> Ignored Layer {current_layer}: Air area ({layer_air_bridge_area:.1f}mm²) below threshold.")
                    
                layer_air_bridge_area = 0.0
                current_layer += 1
                in_bridge_block = False
                continue

            if line.startswith(';TYPE:'):
                type_name = line.strip()
                in_bridge_block = (type_name == ';TYPE:Bridge infill')

            if is_printing_move(line):
                new_x, new_y = get_xy(line, last_x, last_y)
                
                if current_layer not in layer_printed_grids:
                    layer_printed_grids[current_layer] = set()
                
                cells_line = rasterize_line(last_x, last_y, new_x, new_y, GRID_RES_MM)
                layer_printed_grids[current_layer].update(cells_line)
                
                if in_bridge_block:
                    prev_layer_grid = layer_printed_grids.get(current_layer - 1, set())
                    dist = math.hypot(new_x - last_x, new_y - last_y)
                    
                    if dist > 0:
                        steps = max(1, int(dist / 0.5))
                        max_continuous_void = 0.0
                        current_void_run = 0.0
                        
                        for step_i in range(steps + 1):
                            t = step_i / steps
                            sx = last_x + t * (new_x - last_x)
                            sy = last_y + t * (new_y - last_y)
                            cell = (int(sx / GRID_RES_MM), int(sy / GRID_RES_MM))
                            
                            if cell not in prev_layer_grid:
                                current_void_run += (dist / steps)
                                max_continuous_void = max(max_continuous_void, current_void_run)
                            else:
                                current_void_run = 0.0
                        
                        if max_continuous_void >= MIN_CONTINUOUS_VOID_MM:
                            layer_air_bridge_area += dist * 0.45

                last_x, last_y = new_x, new_y
            elif 'X' in line or 'Y' in line:
                last_x, last_y = get_xy(line, last_x, last_y)

        if not target_layers:
            log("No true open-air bridges detected. Exiting cleanly.")
            return

        summary_lines = [f"Found {len(target_layers)} layers with TRUE open-air bridges (> {AREA_THRESHOLD_MM2}mm²):\n"]
        for lr, data in target_layers.items():
            total_area = sum(data['areas'])
            summary_lines.append(f"• Layer {lr} (Z:{data['z']:.2f}mm) - {total_area:.0f}mm² - occurs at ~{data['time_est']}")
        
        summary_lines.append("\nModifications to be applied:")
        summary_lines.append("- Infill/Perimeter order safely reversed using Macro-Blocks.")
        summary_lines.append("- Exact spatial coordinate restoration for arcs & external structures.")
        summary_lines.append(f"- Dual-pass zero-extrusion ironing (+{data['lh']*2:.2f}mm knockdown, then flush).")
        summary_lines.append(f"- Nozzle cooled by {TEMP_DROP_C}°C during ironing to prevent heat creep.")
        
        ui_text = "\n".join(summary_lines)
        log(f"\n[UI PROMPT]\n{ui_text}")
        
        proceed = show_interactive_dialog(ui_text, UI_TIMEOUT_SEC, log)
        if not proceed:
            log("\nUser canceled export via UI. Exiting.")
            sys.exit(1)

        # ==========================================
        # PASS 2: EXECUTE MODIFICATIONS
        # ==========================================
        log("\n[PASS 2] Modifying targeted layers...")
        
        out_lines = []
        global_x, global_y = 0.0, 0.0
        
        current_layer = 0
        layer_buffer = []
        
        for line in lines:
            if line.startswith(';LAYER_CHANGE') or line.startswith('; LAYER_CHANGE'):
                # Process and flush the completed layer buffer before starting the new one
                if layer_buffer:
                    if current_layer in target_layers:
                        layer_out, global_x, global_y = process_target_layer(
                            layer_buffer, global_x, global_y, target_layers[current_layer])
                        out_lines.extend(layer_out)
                    else:
                        out_lines.extend(layer_buffer)
                        # Keep tracker synced on non-target layers so we know where we are entering the next layer
                        for bline in layer_buffer:
                            if is_printing_move(bline) or 'X' in bline or 'Y' in bline:
                                global_x, global_y = get_xy(bline, global_x, global_y)
                
                # Start fresh buffer with the layer change tag
                layer_buffer = [line]
                current_layer += 1
            else:
                layer_buffer.append(line)
        
        # Flush the final layer when EOF is reached
        if layer_buffer:
            if current_layer in target_layers:
                layer_out, global_x, global_y = process_target_layer(
                    layer_buffer, global_x, global_y, target_layers[current_layer])
                out_lines.extend(layer_out)
            else:
                out_lines.extend(layer_buffer)

        log("\n--- Script Completed Successfully ---")
        with open(file_path, 'w', encoding='utf-8', errors='replace') as f:
            f.writelines(out_lines)

    if __name__ == "__main__":
        if len(sys.argv) > 1 and not sys.argv[1].endswith('.bgcode'):
            process_gcode(sys.argv[1])

except Exception as e:
    with open(FATAL_LOG_PATH, 'w') as log_file:
        log_file.write(f"FATAL TOP-LEVEL ERROR at {datetime.now()}:\n")
        log_file.write(traceback.format_exc())
    sys.exit(1)