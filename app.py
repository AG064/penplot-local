#!/usr/bin/env python3
"""Local Tk desktop interface. It exports files only and never connects to a printer."""
from __future__ import annotations
import dataclasses
import datetime
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
from penplot import Settings, convert

class App:
    def __init__(self,root:tk.Tk):
        self.root=root;root.title('PenPlot Local | A1 file exporter');root.geometry('1180x850')
        self.events=queue.Queue();self.worker=None;self.last_out=None;self.preview_image=None
        self.values={};self.source=tk.StringVar();self.parent=tk.StringVar(value=str(Path.home()/'Downloads'))
        self.ack=tk.BooleanVar(value=False);self.strict=tk.BooleanVar(value=True);self.measured=tk.BooleanVar(value=False)
        outer=ttk.Frame(root,padding=20);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='PenPlot Local',font=('TkDefaultFont',22,'bold')).pack(anchor='w')
        ttk.Label(outer,text='PDF, image or ASCII to pen paths. Full-size Bambu Lab A1. Local export only.').pack(anchor='w',pady=(4,15))
        panel=ttk.Frame(outer);panel.pack(fill='both',expand=True)
        form=ttk.Frame(panel,width=435);form.pack(side='left',fill='y',padx=(0,24))
        view=ttk.Frame(panel);view.pack(side='right',fill='both',expand=True)
        ttk.Label(form,text='Input file').grid(row=0,column=0,sticky='w')
        ttk.Entry(form,textvariable=self.source,width=43).grid(row=1,column=0,sticky='ew')
        ttk.Button(form,text='Browse',command=self.pick).grid(row=1,column=1,padx=5)
        ttk.Label(form,text='Output parent folder; a NEW job folder will be created').grid(row=2,column=0,columnspan=2,sticky='w',pady=(10,0))
        ttk.Entry(form,textvariable=self.parent).grid(row=3,column=0,sticky='ew')
        ttk.Button(form,text='Folder',command=self.folder).grid(row=3,column=1,padx=5)
        fields=[('page','PDF page (1-based)','1'),('width_mm','Ink width, mm (blank = PDF native size)',''),
            ('pen_mm','Actual ink-line width, mm (measure it)',''),('z_down','Native nozzle Z at light paper contact',''),
            ('lift_mm','Pen lift, mm','2'),('center_x','Centre X, mm','128'),('center_y','Centre Y, mm','128'),
            ('offset_x','Measured pen-minus-nozzle X, mm',''),('offset_y','Measured pen-minus-nozzle Y, mm',''),
            ('threshold','Dark/white threshold, 1..254','160')]
        for row,(key,title,default) in enumerate(fields,4):
            self.values[key]=tk.StringVar(value=default)
            ttk.Label(form,text=title).grid(row=row,column=0,sticky='w',pady=5)
            ttk.Entry(form,textvariable=self.values[key],width=10).grid(row=row,column=1,sticky='e',padx=5)
        ttk.Checkbutton(form,text='Use both measured XY offsets (otherwise nozzle-centred)',variable=self.measured).grid(row=14,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(form,text='Rendering').grid(row=15,column=0,sticky='w')
        self.mode=tk.StringVar(value='filled')
        ttk.Combobox(form,textvariable=self.mode,values=['filled','outline'],state='readonly',width=10).grid(row=15,column=1,padx=5)
        ttk.Checkbutton(form,text='Strict shipping-label check: source + output codes must decode',variable=self.strict).grid(row=16,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(form,text='For art/ASCII with no barcode, turn strict label checking OFF.\nFilled mode preserves solid ink. Outline is for artwork only.',wraplength=440).grid(row=17,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Checkbutton(form,text='I understand calibration, clearance and unchanged G-code execution.\nNothing here homes, calibrates or controls the printer.',variable=self.ack).grid(row=18,column=0,columnspan=2,sticky='w',pady=10)
        self.generate=ttk.Button(form,text='Generate files and preview',command=self.run);self.generate.grid(row=19,column=0,columnspan=2,sticky='ew')
        self.open_btn=ttk.Button(form,text='Open completed job folder',command=self.open_folder,state='disabled');self.open_btn.grid(row=20,column=0,columnspan=2,sticky='ew',pady=8)
        self.status=tk.StringVar(value='600 DPI. Draw 10 mm/s, travel 20 mm/s, Z 3 mm/s.\nNo auto-fit, no cloud upload, no printer sender.')
        ttk.Label(view,textvariable=self.status,wraplength=620,justify='left').pack(side='bottom',fill='x',pady=8)
        ttk.Label(view,text='Ideal pen footprint, simulated from exported G-code',font=('TkDefaultFont',11,'bold')).pack(anchor='w')
        self.preview=ttk.Label(view,text='No conversion yet.\nThe preview does not certify a physical paper scan.',anchor='center')
        self.preview.pack(fill='both',expand=True,pady=10)
        self.root.after(100,self.poll)
    def pick(self):
        name=filedialog.askopenfilename(filetypes=[('Supported files','*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.txt *.asc'),('All files','*.*')])
        if name:self.source.set(name)
    def folder(self):
        name=filedialog.askdirectory(initialdir=self.parent.get())
        if name:self.parent.set(name)
    def settings(self)->Settings:
        v={k:s.get().strip() for k,s in self.values.items()}
        if not v['pen_mm'] or not v['z_down']:raise ValueError('Enter actual ink width and calibrated pen-contact Z.')
        if self.measured.get() and (not v['offset_x'] or not v['offset_y']):raise ValueError('Both measured offsets are required.')
        cfg=Settings(page=int(v['page']),width_mm=float(v['width_mm']) if v['width_mm'] else None,
            pen_mm=float(v['pen_mm']),z_down=float(v['z_down']),lift_mm=float(v['lift_mm']),
            center_x=float(v['center_x']),center_y=float(v['center_y']),threshold=int(v['threshold']),
            offset_x=float(v['offset_x']) if self.measured.get() else None,
            offset_y=float(v['offset_y']) if self.measured.get() else None,
            mode=self.mode.get(),require_symbols=self.strict.get())
        cfg.validate();return cfg
    def run(self):
        if self.worker and self.worker.is_alive():return
        try:
            if not self.ack.get():raise ValueError('Acknowledge setup requirements before export.')
            cfg=self.settings();source=Path(self.source.get());parent=Path(self.parent.get())
            if not parent.is_dir():raise ValueError('Select an existing output parent folder.')
            out=parent/('PenPlot_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        except Exception as exc:messagebox.showerror('Check settings',str(exc));return
        self.generate.configure(state='disabled');self.open_btn.configure(state='disabled')
        self.status.set('Converting locally. No command is being sent to the printer.\nComplex images can take a while.')
        def job():
            try:self.events.put(('ok',out,convert(source,out,cfg)))
            except Exception as exc:self.events.put(('error',str(exc)))
        self.worker=threading.Thread(target=job,daemon=True);self.worker.start()
    def poll(self):
        try:
            event=self.events.get_nowait();self.generate.configure(state='normal')
            if event[0]=='error':self.status.set('Conversion stopped. '+event[1]);messagebox.showerror('Conversion stopped',event[1])
            else:
                _,out,m=event;self.last_out=out;self.open_btn.configure(state='normal')
                im=Image.open(out/'SIMULATED_INK.png');im.thumbnail((620,550))
                self.preview_image=ImageTk.PhotoImage(im);self.preview.configure(image=self.preview_image,text='')
                c=m['gcode_checks'];size=m['source']['output_canvas_mm']
                self.status.set(f"Files saved locally: {out.name}\nCanvas {size[0]:.2f} x {size[1]:.2f} mm; {c['pen_down_strokes']} strokes.\nNominal motion {c['nominal_seconds_excluding_initial_positioning_acceleration_and_firmware']/60:.1f} min + overhead.\nSymbol check: {m['quality']['decode_gate']}\nInspect preview. Physical pen, clearance and paper scan remain unverified.")
        except queue.Empty:pass
        self.root.after(100,self.poll)
    def open_folder(self):
        if not self.last_out:return
        if sys.platform=='win32':os.startfile(self.last_out)
        elif sys.platform=='darwin':subprocess.Popen(['open',str(self.last_out)])
        else:subprocess.Popen(['xdg-open',str(self.last_out)])

if __name__=='__main__':
    root=tk.Tk();App(root);root.mainloop()
