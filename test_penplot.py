from __future__ import annotations
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw
import penplot as p

class Tests(unittest.TestCase):
    def test_native_transform_flips_y_once(self):
        cfg=p.Settings();q=p.map_native([np.array([[0,0],[100,50]])],[100,50],cfg)[0]
        np.testing.assert_allclose(q,[[78,153],[178,103]])
    def test_measured_offset_sign(self):
        q=p.map_native([np.array([[50.,25.]])],[100,50],p.Settings(offset_x=8,offset_y=-12))[0]
        np.testing.assert_allclose(q,[[120,140]])
    def test_bounds_reject_no_auto_fit(self):
        with self.assertRaises(ValueError):p.map_native([np.array([[0.,0.],[250.,250.]])],[250,250],p.Settings())
    def test_nonfinite_and_bad_settings(self):
        for kw in [dict(z_down=float('nan')),dict(pen_mm=0),dict(lift_mm=-1),dict(offset_x=2),dict(draw_mm_s=200)]:
            with self.assertRaises(ValueError):p.Settings(**kw).validate()
    def test_restricts_unsafe_commands(self):
        cfg=p.Settings();g=p.gcode([np.array([[128.,128.],[130.,128.]])],cfg)
        for extra in ['G28','M104 S200','G92 Z0','M84','G1 X128 Y128 E1 F600','G1 Z-1 F180','M211 S0']:
            with self.assertRaises(ValueError):p.parse_and_validate(g+'\n'+extra+'\n',cfg)
    def test_export_roundtrip_and_finish_lift(self):
        cfg=p.Settings();paths=[np.array([[128.,128.],[140.,128.],[140.,140.],[128.,128.]])]
        parsed,c=p.parse_and_validate(p.gcode(paths,cfg),cfg)
        np.testing.assert_allclose(parsed[0],paths[0]);self.assertEqual(c['z_values'],[10.,12.,15.])
    def test_air_never_touches(self):
        cfg=p.Settings();parsed,c=p.parse_and_validate(p.gcode([np.array([[120.,120.],[130.,120.]])],cfg,air=True),cfg,air=True)
        self.assertEqual(parsed,[]);self.assertEqual(c['z_values'],[12.,15.])
    def test_skeleton_closed_loop_keeps_closing_edge(self):
        sk=np.zeros((60,80),bool);sk[10,10:71]=True;sk[50,10:71]=True;sk[10:51,10]=True;sk[10:51,70]=True
        paths=p.skeleton_paths(sk)
        self.assertTrue(any(np.array_equal(q[0],q[-1]) for q in paths if len(q)>2))
        canvas=Image.new('L',(80,60),0);d=ImageDraw.Draw(canvas)
        for q in paths:
            if len(q)>1:d.line([tuple(v) for v in q],fill=255,width=1)
        self.assertGreater(np.logical_and(np.asarray(canvas)>0,sk).sum()/sk.sum(),.99)
    def test_branching_rules_preserved(self):
        sk=np.zeros((60,80),bool);sk[10,10:71]=True;sk[50,10:71]=True;sk[10:51,10]=True;sk[10:51,70]=True;sk[30,10:71]=True
        canvas=Image.new('L',(80,60),0);d=ImageDraw.Draw(canvas)
        for q in p.skeleton_paths(sk):
            if len(q)>1:d.line([tuple(v) for v in q],fill=255,width=1)
        self.assertTrue(np.all(np.asarray(canvas)[30,11:70]>0))
    def test_single_dot_survives(self):
        sk=np.zeros((20,20),bool);sk[10,10]=True
        ps=p.skeleton_paths(sk);self.assertEqual(len(ps),1);self.assertEqual(len(ps[0]),1)
    def test_ascii_tabs_and_layout(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.txt';f.write_text('A\tB\n  C',encoding='utf-8')
            im,info=p.render_ascii(f);self.assertEqual(info['columns'],9);self.assertEqual(info['rows'],2)
            grey,m=p.load_source(f,p.Settings(width_mm=60));self.assertEqual(m['kind'],'ascii');self.assertGreater(grey.size,0)
    def test_ascii_rejects_ansi_and_non_ascii(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.asc'
            for t in ['A\x1b[31mB','Привет']:
                f.write_text(t,encoding='utf-8')
                with self.assertRaises(ValueError):p.render_ascii(f)
    def test_image_requires_explicit_size(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';Image.new('L',(30,30),0).save(f)
            with self.assertRaises(ValueError):p.load_source(f,p.Settings())
    def test_transparent_pixels_are_white_not_black(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';im=Image.new('RGBA',(100,80),(0,0,0,0));ImageDraw.Draw(im).rectangle((40,30,60,50),fill=(0,0,0,255));im.save(f)
            _,m=p.load_source(f,p.Settings(width_mm=30));self.assertEqual(m['crop_ink_rect_px'],[40,30,61,51])
    def test_blank_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';Image.new('L',(30,30),255).save(f)
            with self.assertRaises(ValueError):p.load_source(f,p.Settings(width_mm=30))
    def test_pdf_scale_and_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            doc=fitz.open();pg=doc.new_page(width=200,height=300);pg.draw_rect(fitz.Rect(20,30,100,70),color=None,fill=(0,0,0))
            f=Path(td)/'a.pdf';doc.save(f)
            _,m=p.load_source(f,p.Settings(dpi=300));w,h=m['native_ink_size_mm']
            self.assertAlmostEqual(w,80*25.4/72,delta=.15);self.assertAlmostEqual(h,40*25.4/72,delta=.15)
            pg.set_rotation(90);r=Path(td)/'r.pdf';doc.save(r);doc.close()
            _,m=p.load_source(r,p.Settings(dpi=300));rw,rh=m['native_ink_size_mm']
            self.assertAlmostEqual(rw,h,delta=.15);self.assertAlmostEqual(rh,w,delta=.15)
    def test_converter_png_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';im=Image.new('L',(150,100),255);ImageDraw.Draw(im).rectangle((20,20,130,80),outline=0,width=4);im.save(f)
            out=Path(td)/'job';m=p.convert(f,out,p.Settings(width_mm=30,dpi=300))
            self.assertTrue((out/'02_FULL_LABEL_OR_ART_CALIBRATE_FIRST.gcode').is_file());self.assertEqual(m['gcode_checks']['status'],'PASS_STATIC_ONLY')
            with self.assertRaises(FileExistsError):p.convert(f,out,p.Settings(width_mm=30,dpi=300))
    def test_strict_requires_decoder_and_codes(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';Image.new('L',(40,40),0).save(f)
            with patch('penplot.available_decoder',return_value=(None,'test unavailable')):
                with self.assertRaises(ValueError):p.convert(f,Path(td)/'out',p.Settings(width_mm=10,require_symbols=True,dpi=300))
    def test_barcode_outline_mode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'a.png';Image.new('L',(40,40),0).save(f)
            with patch('penplot.decode_image',return_value={'status':'RAN','symbols':[{'type':'CODE128','payload_hex':'41'}]}):
                with self.assertRaises(ValueError):p.convert(f,Path(td)/'out',p.Settings(width_mm=10,mode='outline',dpi=300))
    def test_path_planner_keeps_geometry(self):
        paths=[np.array([[50.,50.],[51.,50.]]),np.array([[2.,2.],[1.,1.]])]
        ordered=p.order_paths(paths)
        expected=sorted(tuple(sorted(map(tuple,q))) for q in paths);actual=sorted(tuple(sorted(map(tuple,q))) for q in ordered)
        self.assertEqual(actual,expected)

if __name__=='__main__':unittest.main(verbosity=2)
