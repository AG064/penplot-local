#!/usr/bin/env python3
"""Local, deterministic raster-to-pen-path reference implementation.
No network, printer connection, OCR, barcode recreation, or slicer start/end code.
Run --help for CLI usage. See README.md before using motion files.
"""
from __future__ import annotations
import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any
import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy.ndimage import distance_transform_edt
from skimage.morphology import skeletonize

VERSION = '0.1.1'
MAX_PIXELS = 24_000_000
MAX_BYTES = 32 * 1024 * 1024
MAX_PATHS = 8_000
MAX_POINTS = 1_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8', newline='\n')


@dataclasses.dataclass(frozen=True)
class Settings:
    dpi: int = 600
    threshold: int = 160
    pen_mm: float = 0.30
    width_mm: float | None = None  # None preserves PDF physical scale; images require width.
    margin_mm: float = 2.0
    page: int = 1
    z_down: float = 10.0
    lift_mm: float = 2.0
    center_x: float = 128.0
    center_y: float = 128.0
    offset_x: float | None = None
    offset_y: float | None = None
    draw_mm_s: float = 10.0
    travel_mm_s: float = 20.0
    z_mm_s: float = 3.0
    acceleration: float = 300.0
    mode: str = 'filled'
    require_symbols: bool = False

    def validate(self) -> None:
        limits = {'dpi': (300,600), 'threshold': (1,254), 'pen_mm': (.15,1.0),
            'margin_mm': (1,10), 'z_down': (2,60), 'lift_mm': (1,5),
            'center_x': (20,236), 'center_y': (20,236), 'draw_mm_s': (2,20),
            'travel_mm_s': (2,30), 'z_mm_s': (.5,5), 'acceleration': (50,500)}
        for key, (low, high) in limits.items():
            v = getattr(self, key)
            if not math.isfinite(v) or not low <= v <= high:
                raise ValueError(f'{key} must be finite and within {low}..{high}.')
        if not isinstance(self.dpi, int) or not isinstance(self.page, int) or self.page < 1:
            raise ValueError('DPI and page must be integers; page is 1-based.')
        if self.mode not in ('filled','outline'):
            raise ValueError('mode must be filled or outline.')
        if self.width_mm is not None and (not math.isfinite(self.width_mm) or not 10 <= self.width_mm <= 200):
            raise ValueError('width_mm must be 10..200 mm.')
        if (self.offset_x is None) != (self.offset_y is None):
            raise ValueError('Provide BOTH measured pen-minus-nozzle offsets, or neither.')
        for v in (self.offset_x,self.offset_y):
            if v is not None and (not math.isfinite(v) or abs(v)>70):
                raise ValueError('Measured offsets must be finite, within +/-70 mm.')


def available_decoder() -> tuple[Any, str | None]:
    try:
        from pyzbar.pyzbar import decode
        return decode, None
    except (ImportError, OSError) as exc:
        return None, str(exc)


def decode_image(image: Image.Image) -> dict:
    decode, error = available_decoder()
    if decode is None:
        return {'status':'UNAVAILABLE', 'reason':error, 'symbols':[]}
    symbols = []
    for b in decode(image.convert('L')):
        symbols.append({'type':b.type, 'payload_hex':b.data.hex(),
            'payload_sha256':sha256(b.data),
            'rect_px':[b.rect.left,b.rect.top,b.rect.width,b.rect.height]})
    symbols.sort(key=lambda b:(b['type'], b['payload_hex'], b['rect_px']))
    return {'status':'RAN', 'symbols':symbols}


def signatures(decoded: dict) -> list[tuple[str,str]]:
    return sorted((s['type'],s['payload_hex']) for s in decoded['symbols'])


def render_ascii(path: Path) -> tuple[Image.Image,dict]:
    text = path.read_text(encoding='utf-8-sig').replace('\r\n','\n').replace('\r','\n')
    if any(ord(c)>126 or (ord(c)<32 and c not in '\n\t') for c in text):
        raise ValueError('ASCII mode accepts printable ASCII, tabs and newlines only. No ANSI escapes or Unicode in v0.1.')
    lines=text.expandtabs(8).split('\n')
    cols=max(map(len,lines),default=0)
    if not cols or len(lines)>250 or cols>250:
        raise ValueError('ASCII input must contain ink and fit within 250 columns x 250 rows.')
    # Read a locally installed font. No font file is copied into the output.
    candidates=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/consola.ttf',
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'),
        Path('/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf'),
        Path('/System/Library/Fonts/Menlo.ttc')]
    font_path=next((p for p in candidates if p.is_file()),None)
    if font_path is None:
        raise ValueError('No supported local monospaced font found. Install a monospace font listed in README.md.')
    font=ImageFont.truetype(str(font_path),24)
    cw=math.ceil(font.getlength('M')); ch=32
    size=(cols*cw+16,len(lines)*ch+16)
    if size[0]*size[1]>MAX_PIXELS: raise ValueError('ASCII raster exceeds pixel limit.')
    im=Image.new('L',size,255); dr=ImageDraw.Draw(im)
    for row,line in enumerate(lines):
        dr.text((8,8+row*ch),line,font=font,fill=0,anchor='lt')
    return im, {'columns':cols,'rows':len(lines),'tab_width':8,
        'font_name':font_path.name, 'font_sha256':sha256(font_path.read_bytes()),
        'cell_px':[cw,ch], 'layout':'monospaced; line order and tab expansion preserved'}


def load_source(path: Path, cfg: Settings) -> tuple[np.ndarray,dict]:
    """Rasterize a PDF at native scale, or a bitmap/ASCII at explicit width.
    White crop margins are geometric cropping only; no content rewriting.
    """
    cfg.validate()
    if not path.is_file() or path.stat().st_size>MAX_BYTES:
        raise ValueError('Input missing or exceeds 32 MiB.')
    source={'name':path.name,'sha256':sha256(path.read_bytes()),'bytes':path.stat().st_size}
    ppm=cfg.dpi/25.4
    if path.suffix.lower()=='.pdf':
        with fitz.open(path) as doc:
            if doc.needs_pass: raise ValueError('Encrypted PDF requires an unlocked copy.')
            if cfg.page>len(doc): raise ValueError('Selected PDF page does not exist.')
            pg=doc[cfg.page-1]
            # Bounded low-resolution render for content crop; retains the page rotation.
            z=min(2.0,2048/max(pg.rect.width,pg.rect.height))
            pix=pg.get_pixmap(matrix=fitz.Matrix(z,z),colorspace=fitz.csGRAY,alpha=False,annots=True)
            lo=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width)
            yy,xx=np.nonzero(lo<cfg.threshold)
            if len(xx)==0: raise ValueError('Blank page at selected threshold.')
            # Expand by a millimetre here so antialiased edges cannot be clipped.
            pad=72/25.4
            clip=fitz.Rect((xx.min()+pix.x)/z-pad,(yy.min()+pix.y)/z-pad,
                          (xx.max()+1+pix.x)/z+pad,(yy.max()+1+pix.y)/z+pad)&pg.rect
            # Handle rotated pages through the renderer, not textual bounding boxes.
            scale=cfg.dpi/72
            if math.ceil(clip.width*scale)*math.ceil(clip.height*scale)>MAX_PIXELS:
                raise ValueError('Selected ink region exceeds raster limit at this DPI.')
            pix=pg.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,colorspace=fitz.csGRAY,alpha=False,annots=True)
            grey=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width).copy()
            if grey.size>MAX_PIXELS: raise ValueError('PDF raster exceeds pixel limit.')
            yy,xx=np.nonzero(grey<cfg.threshold)
            if not len(xx): raise ValueError('Blank page after rendering.')
            ink_rect=[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)]
            ink_page_mm=[(pix.x+ink_rect[0])/ppm,(pix.y+ink_rect[1])/ppm,
                         (pix.x+ink_rect[2])/ppm,(pix.y+ink_rect[3])/ppm]
            source.update(kind='pdf',page_count=len(doc),selected_page=cfg.page,
                page_size_mm=[pg.rect.width*25.4/72,pg.rect.height*25.4/72],
                rotation_degrees=pg.rotation, native_ink_bbox_mm=ink_page_mm,
                render_dpi=cfg.dpi, pdf_embedded_text_used='renderer only; not retyped',
                ocr_used=False, vector_primitive_count=len(pg.get_drawings()))
    else:
        if cfg.width_mm is None:
            raise ValueError('Bitmap and ASCII inputs require an explicit ink width in mm; DPI metadata is not guessed.')
        if path.suffix.lower() in ('.txt','.asc'):
            im,ascii_info=render_ascii(path); source.update(kind='ascii',ascii=ascii_info)
        else:
            if path.suffix.lower() not in ('.png','.jpg','.jpeg','.bmp','.webp','.tif','.tiff'):
                raise ValueError('Supported: PDF, PNG, JPEG, BMP, WebP, single-frame TIFF, ASCII TXT/ASC.')
            with Image.open(path) as src:
                if getattr(src,'n_frames',1)!=1:
                    raise ValueError('Multi-frame image needs an explicitly selected single frame first.')
                if src.width*src.height>MAX_PIXELS: raise ValueError('Image exceeds pixel limit.')
                rgba=ImageOps.exif_transpose(src).convert('RGBA')
                white=Image.new('RGBA',rgba.size,(255,255,255,255))
                im=Image.alpha_composite(white,rgba).convert('L')
            source.update(kind='bitmap',input_size_px=list(im.size),alpha_composited_on='white',exif_orientation_applied=True)
        grey=np.asarray(im).copy()
        yy,xx=np.nonzero(grey<cfg.threshold)
        if not len(xx): raise ValueError('No dark content at selected threshold.')
        ink_rect=[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)]
    x0,y0,x1,y1=ink_rect
    grey=grey[y0:y1,x0:x1]
    native_size=[grey.shape[1]/ppm,grey.shape[0]/ppm] if source['kind']=='pdf' else None
    scale_factor=1.0
    if cfg.width_mm is not None:
        w=round(cfg.width_mm*ppm); h=max(1,round(w*grey.shape[0]/grey.shape[1]))
        if w*h>MAX_PIXELS: raise ValueError('Scaled raster exceeds pixel limit.')
        scale_factor=cfg.width_mm/native_size[0] if native_size else None
        grey=cv2.resize(grey,(w,h),interpolation=cv2.INTER_AREA if w<grey.shape[1] else cv2.INTER_CUBIC)
    border=math.ceil(cfg.margin_mm*ppm)
    if (grey.shape[0]+2*border)*(grey.shape[1]+2*border)>MAX_PIXELS:
        raise ValueError('Padded raster exceeds pixel limit.')
    grey=cv2.copyMakeBorder(grey,border,border,border,border,cv2.BORDER_CONSTANT,value=255)
    source.update(native_ink_size_mm=native_size,applied_scale=scale_factor,
        crop_ink_rect_px=ink_rect,margin_px=border,margin_actual_mm=border/ppm,
        output_size_px=[grey.shape[1],grey.shape[0]],
        output_canvas_mm=[grey.shape[1]/ppm,grey.shape[0]/ppm],
        output_ink_size_mm=[(grey.shape[1]-2*border)/ppm,(grey.shape[0]-2*border)/ppm])
    return grey,source


def skeleton_paths(sk: np.ndarray) -> list[np.ndarray]:
    """Trace an 8-neighbour pixel graph; suppress redundant diagonal shortcuts.
    Every graph edge is visited. No joining over unrelated white space.
    """
    ys,xs=np.nonzero(sk)
    w=sk.shape[1]
    nodes={int(y)*w+int(x) for y,x in zip(ys,xs)}
    if len(nodes)>MAX_POINTS: raise ValueError('Skeleton exceeds point limit.')
    graph={n:set() for n in nodes}
    for n in nodes:
        y,x=divmod(n,w)
        for dy,dx in [(0,1),(1,0),(1,1),(1,-1)]:
            ny,nx=y+dy,x+dx
            if not (0<=nx<w and 0<=ny<sk.shape[0]):continue
            q=ny*w+nx
            if q not in nodes:continue
            if dx and dy and (y*w+nx in nodes or ny*w+x in nodes):continue
            graph[n].add(q);graph[q].add(n)
    paths=[]
    for n in sorted(nodes):
        if not graph[n]: paths.append(np.array([[n%w,n//w]],np.float64))
    # Starts at graph endpoints/junctions, then covers any remaining cycles.
    starts=sorted(nodes,key=lambda n:(len(graph[n])!=1,len(graph[n])==2,n))
    for start in starts:
        while graph[start]:
            seq=[start];cur=start;previous=None
            while graph[cur]:
                candidates=sorted(graph[cur])
                if previous is None:q=candidates[0]
                else:
                    cy,cx=divmod(cur,w);py,px=divmod(previous,w)
                    vx,vy=cx-px,cy-py
                    def continuation(k):
                        ky,kx=divmod(k,w);dx,dy=kx-cx,ky-cy
                        return ((vx*dx+vy*dy)/max(math.hypot(dx,dy),1),-k)
                    q=max(candidates,key=continuation)
                graph[cur].remove(q);graph[q].remove(cur)
                seq.append(q);previous,cur=cur,q
                if cur==start:break
            pts=np.array([[k%w,k//w] for k in seq],np.float64)
            if len(pts)>2:
                closed = bool(np.array_equal(pts[0], pts[-1]))
                pts=cv2.approxPolyDP(pts.astype(np.float32),.45,closed).reshape(-1,2).astype(float)
                if closed and not np.array_equal(pts[0],pts[-1]):
                    pts=np.vstack([pts,pts[0]])
            paths.append(pts)
    return paths


def build_paths(mask: np.ndarray, cfg: Settings) -> tuple[list[np.ndarray],dict]:
    ppm=cfg.dpi/25.4
    info={'algorithm':'inset contour fill + original-mask skeleton; no OCR or symbol regeneration',
          'threshold':cfg.threshold,'pen_mm':cfg.pen_mm,'pixel_mm':1/ppm,
          'contour_step_mm':cfg.pen_mm*.70,'simplification_mm_max':.45/ppm}
    out=[];contour_count=0
    if cfg.mode=='outline':
        cc,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        for c in cc:
            p=cv2.approxPolyDP(c,.45,True).reshape(-1,2).astype(float)
            if len(p)>1:p=np.vstack([p,p[0]])
            out.append(p)
        info.update(contour_paths=len(out),skeleton_paths=0,
            warning='OUTLINE IS ART MODE; not an ink-faithful fill and unsuitable for barcodes/QR codes.')
    else:
        distance=distance_transform_edt(mask)
        radius=cfg.pen_mm*ppm/2
        level=radius+.5  # Distance-to-pixel-centre correction; errs inward.
        maximum=float(distance.max());levels=0
        while level<=maximum:
            eroded=(distance>=level).astype(np.uint8)
            cc,_=cv2.findContours(eroded,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
            for c in cc:
                p=cv2.approxPolyDP(c,.45,True).reshape(-1,2).astype(float)
                if len(p)>1:p=np.vstack([p,p[0]])
                out.append(p)
            contour_count+=len(cc);levels+=1;level+=cfg.pen_mm*.70*ppm
            if len(out)>MAX_PATHS:raise ValueError('Too many paths; use a smaller image or simpler art.')
        sk=skeletonize(mask)
        spine=skeleton_paths(sk)
        # Centreline strokes keep narrow rules, text strokes, dots and diacritics.
        # They can be wider than the original when the selected pen is too broad.
        out+=spine
        info.update(contour_paths=contour_count,contour_levels=levels,skeleton_paths=len(spine),
            skeleton_pixels=int(sk.sum()), thin_skeleton_fraction=float(np.mean(distance[sk]<radius)) if sk.any() else 0)
    out=[p for p in out if len(p)]
    if len(out)>MAX_PATHS or sum(map(len,out))>MAX_POINTS:raise ValueError('Path/vertex budget exceeded.')
    # Pixel centre to local physical top-left coordinates.
    paths=[(p+.5)/ppm for p in out]
    info.update(paths=len(paths),vertices=sum(map(len,paths)))
    return paths,info


def order_paths(paths: list[np.ndarray]) -> list[np.ndarray]:
    """Deterministic nearest endpoint, direction reversal only; never draw connectors."""
    if not paths:return []
    # Endpoints of closed curves are not moved to another vertex: shape is invariant.
    first=np.array([p[0] for p in paths]);last=np.array([p[-1] for p in paths])
    active=np.ones(len(paths),bool); current=np.array([0.,0.]);out=[]
    for _ in range(len(paths)):
        a=np.sum((first-current)**2,axis=1);b=np.sum((last-current)**2,axis=1)
        a[~active]=np.inf;b[~active]=np.inf
        ia=int(a.argmin());ib=int(b.argmin())
        reverse=b[ib]<a[ia];i=ib if reverse else ia
        p=paths[i][::-1].copy() if reverse else paths[i].copy()
        out.append(p);active[i]=False;current=p[-1]
    return out


def map_native(paths: list[np.ndarray],size_mm: list[float],cfg: Settings) -> list[np.ndarray]:
    w,h=size_mm;dx=cfg.offset_x or 0.;dy=cfg.offset_y or 0.
    out=[]
    for path in paths:
        p=np.column_stack([cfg.center_x-dx+path[:,0]-w/2,cfg.center_y-dy+h/2-path[:,1]])
        if not np.isfinite(p).all() or (p<20-1e-8).any() or (p>236+1e-8).any():
            raise ValueError('Nozzle path leaves the selected A1 software guard X/Y 20..236. Do not auto-shrink.')
        if cfg.offset_x is not None:
            pen=p+np.array([dx,dy])
            if (pen<20).any() or (pen>236).any():raise ValueError('Measured pen-tip path leaves the A1 software guard.')
        out.append(p)
    return out


def gcode(paths: list[np.ndarray], cfg: Settings, *, air: bool=False) -> str:
    cfg.validate()
    zu=cfg.z_down+cfg.lift_mm;ze=zu+3
    lines=[f'; PENPLOT {VERSION} / FULL-SIZE A1 / CALIBRATION-DEPENDENT',
        '; Cold, already homed in native coordinates. No automatic homing.',
        '; Pen and holder must be removed during homing. No normal slicer wrapper.',
        '; No sender origin shift. XY offsets UNKNOWN unless measured in manifest.',
        f'; Requires light paper contact at nozzle Z{cfg.z_down:.3f}; clearance Z{zu:.3f}.',
        f'; Assumed actual ink width {cfg.pen_mm:.3f} mm. This is NOT the pen barrel diameter.',
        '; Geometry and motion checks only; no physical scan/clearance certification.',
        'M104 S0','M140 S0','M106 S0','G21','G90','M220 S100',f'M204 S{cfg.acceleration:g}',
        f'G1 Z{zu:.3f} F{cfg.z_mm_s*60:g} ; clearance before XY']
    for i,p in enumerate(paths):
        lines+=[f'; stroke {i+1}/{len(paths)}',f'G1 X{p[0,0]:.3f} Y{p[0,1]:.3f} F{cfg.travel_mm_s*60:g}']
        if not air:lines.append(f'G1 Z{cfg.z_down:.3f} F{cfg.z_mm_s*60:g}')
        for x,y in p[1:]:lines.append(f'G1 X{x:.3f} Y{y:.3f} F{(cfg.travel_mm_s if air else cfg.draw_mm_s)*60:g}')
        if not air:lines.append(f'G1 Z{zu:.3f} F{cfg.z_mm_s*60:g}')
    lines += [f'G1 Z{ze:.3f} F{cfg.z_mm_s*60:g}','M400','M104 S0','M140 S0','M106 S0',
        '; END: lifted in place. Remove holder before normal printing or homing.']
    return '\n'.join(lines)+'\n'


def parse_and_validate(text: str,cfg: Settings,air: bool=False) -> tuple[list[np.ndarray],dict]:
    """Independent strict parser of EXPORTED bytes. Reject unknown commands/parameters."""
    fixed={'M104':{'S':0.},'M140':{'S':0.},'M106':{'S':0.},'G21':{},'G90':{},
        'M220':{'S':100.},'M204':{'S':cfg.acceleration},'M400':{}}
    x=y=z=None;drawing=[];strokes=[];xy=[];zs=[];pen_length=travel_length=z_known=0.;seconds=0.;down_count=0
    for line_no,raw in enumerate(text.splitlines(),1):
        line=raw.split(';')[0].strip()
        if not line:continue
        parts=line.split();command=parts[0];params={}
        for part in parts[1:]:
            if not re.fullmatch(r'[A-Z][-+]?\d+(?:\.\d+)?',part):raise ValueError(f'Invalid token at {line_no}: {part}')
            if part[0] in params:raise ValueError('Duplicate parameter.')
            params[part[0]]=float(part[1:])
        if command in fixed:
            if params!=fixed[command]:raise ValueError(f'Unsafe or unexpected {command} at {line_no}.')
            continue
        if command!='G1' or not set(params)<=set('XYZF') or 'F' not in params:
            raise ValueError(f'Unknown/unsafe command or parameters at line {line_no}.')
        if params['F']<=0:raise ValueError('Nonpositive feed.')
        if 'Z' in params:
            if set(params)!=set('ZF'):raise ValueError('Mixed Z/XY motion is forbidden.')
            nz=params['Z'];zs.append(nz)
            if not any(abs(nz-v)<1e-6 for v in [cfg.z_down,cfg.z_down+cfg.lift_mm,cfg.z_down+cfg.lift_mm+3]):
                raise ValueError('Unexpected Z coordinate.')
            if air and abs(nz-cfg.z_down)<1e-6:raise ValueError('Air file lowered the pen.')
            if z is None and nz!=cfg.z_down+cfg.lift_mm:raise ValueError('First motion must establish pen clearance.')
            if params['F']>cfg.z_mm_s*60+1e-6:raise ValueError('Z feed exceeds profile.')
            if z is not None:seconds+=abs(nz-z)/(params['F']/60);z_known+=abs(nz-z)
            if abs(nz-cfg.z_down)<1e-6:
                if x is None or y is None:raise ValueError('Pen down without XY position.')
                drawing=[(x,y)];down_count+=1
            elif drawing:
                strokes.append(np.array(drawing));drawing=[]
            z=nz
        else:
            if set(params)!=set('XYF') or z is None:raise ValueError('XY must specify X/Y/F after lift.')
            nx,ny=params['X'],params['Y']
            if not (20<=nx<=236 and 20<=ny<=236):raise ValueError('XY outside the A1 software guard.')
            down=abs(z-cfg.z_down)<1e-6
            if not down and z<cfg.z_down+cfg.lift_mm:raise ValueError('Insufficient travel clearance.')
            if params['F']>(cfg.draw_mm_s if down else cfg.travel_mm_s)*60+1e-6:raise ValueError('XY feed exceeds profile.')
            if x is not None:
                d=math.hypot(nx-x,ny-y);seconds+=d/(params['F']/60)
                if down:pen_length+=d
                else:travel_length+=d
            if down:drawing.append((nx,ny))
            x,y=nx,ny;xy.append((x,y))
    if drawing or z!=cfg.z_down+cfg.lift_mm+3:raise ValueError('Job did not finish lifted.')
    pts=np.asarray(xy)
    return strokes, {'status':'PASS_STATIC_ONLY','gcode_sha256':sha256(text.encode()),'pen_down_strokes':down_count,
        'pen_down_length_mm':pen_length,'pen_up_xy_length_mm':travel_length,'known_z_travel_mm':z_known,
        'nominal_seconds_excluding_initial_positioning_acceleration_and_firmware':seconds,
        'nozzle_bounds_mm':{'min':pts.min(axis=0).tolist(),'max':pts.max(axis=0).tolist()},
        'z_values':sorted(set(zs)), 'no_homing':True,'no_extrusion':True,'no_heater_on':True,
        'no_coordinate_reset':True,'no_motor_disable':True,'physical_tested':False}


def simulate_native(strokes:list[np.ndarray], size_px:tuple[int,int], size_mm:list[float], cfg:Settings,
                    pen_mm:float|None=None) -> Image.Image:
    """Ideal round uniform ink footprint from parsed G-code, not from the source raster."""
    w,h=size_mm;ppm=cfg.dpi/25.4
    im=Image.new('L',size_px,255);draw=ImageDraw.Draw(im)
    # Integer pixel width is reported in the manifest. Not a physical pen model.
    d=max(1,round((pen_mm or cfg.pen_mm)*ppm));rad=(d-1)/2
    for p in strokes:
        x=p[:,0]-(cfg.center_x-(cfg.offset_x or 0.))+w/2
        y=h/2-(p[:,1]-(cfg.center_y-(cfg.offset_y or 0.)))
        pts=list(zip(x*ppm-.5,y*ppm-.5))
        if len(pts)>1:draw.line(pts,fill=0,width=d,joint='curve')
        for px,py in pts:draw.ellipse((px-rad,py-rad,px+rad,py+rad),fill=0)
    return im


def save_svg(path:Path,paths:list[np.ndarray],size_mm:list[float],pen:float)->None:
    w,h=size_mm
    s=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.6f}mm" height="{h:.6f}mm" viewBox="0 0 {w:.6f} {h:.6f}">',
       '<title>Pen centreline paths; not printer G-code</title>',
       f'<g fill="none" stroke="black" stroke-width="{pen:.6f}" stroke-linecap="round" stroke-linejoin="round">']
    for p in paths:
        if len(p)==1:
            s.append(f'<circle cx="{p[0,0]:.6f}" cy="{p[0,1]:.6f}" r="{pen/2:.6f}" fill="black" stroke="none"/>')
        else:s.append('<path d="M '+' L '.join(f'{x:.6f},{y:.6f}' for x,y in p)+'"/>')
    s+=['</g>','</svg>'];path.write_text('\n'.join(s),encoding='utf-8',newline='\n')


def convert(source:Path,out:Path,cfg:Settings)->dict:
    """Prepare a job folder. Does not send or run G-code."""
    cfg.validate()
    if out.exists():raise FileExistsError('Choose a NEW output directory; existing jobs are never overwritten.')
    grey,src=load_source(source,cfg)
    mask=grey<cfg.threshold
    before=decode_image(Image.fromarray(grey))
    if cfg.require_symbols and (before['status'] != 'RAN' or not before['symbols']):
        raise ValueError('Strict label mode requires a working barcode decoder and at least one readable source symbol.')
    if cfg.mode=='outline' and before['symbols']:
        raise ValueError('Detected machine-readable symbols: outline mode is blocked. Use filled mode.')
    local,path_info=build_paths(mask,cfg)
    local=order_paths(local)
    native=map_native(local,src['output_canvas_mm'],cfg)
    full=gcode(native,cfg)
    parsed,checks=parse_and_validate(full,cfg)
    # Check parsed rounded geometry against the planner, including isolated dots.
    if len(parsed)!=len(native) or any(p.shape!=q.shape or np.max(np.abs(p-q))>.000501 for p,q in zip(parsed,native)):
        raise ValueError('Exported G-code does not reproduce planned pen paths within 0.001 mm.')
    sim=simulate_native(parsed,tuple(src['output_size_px']),src['output_canvas_mm'],cfg)
    after=decode_image(sim)
    a=mask;b=np.asarray(sim)<128
    quality={'source_decode':before,'simulated_gcode_decode':after,
        'decode_gate':'PASS_SAME_PAYLOADS' if before['symbols'] and signatures(before)==signatures(after) else
            'FAIL_PAYLOAD_MISMATCH' if before['symbols'] else 'NO_SYMBOLS_DETECTED_OR_DECODER_UNAVAILABLE',
        'ideal_ink_intersection_over_union':float(np.logical_and(a,b).sum()/max(1,np.logical_or(a,b).sum())),
        'source_black_coverage_fraction':float(np.logical_and(a,b).sum()/max(1,a.sum())),
        'ideal_ink_outside_source_fraction':float((b&~a).sum()/max(1,b.sum())),
        'simulated_ink_width_px':round(cfg.pen_mm*cfg.dpi/25.4),
        'simulated_ink_width_mm':round(cfg.pen_mm*cfg.dpi/25.4)*25.4/cfg.dpi,
        'physical_scan_verified':False,'carrier_acceptance_verified':False,
        'warning':'An ideal digital scan is NOT a paper-scan quality grade. Small text may thicken; inspect the preview.'}
    if quality['decode_gate']=='FAIL_PAYLOAD_MISMATCH':
        # Preserve diagnostics, but do not provide motion for a known unreadable result.
        out.mkdir(parents=True);Image.fromarray(grey).save(out/'SOURCE_CROP.png');sim.save(out/'SIMULATED_INK.png')
        write_json(out/'REJECTED_QUALITY.json',quality)
        raise ValueError(f'Symbol round-trip failed; diagnostic images saved in {out}. No G-code exported.')
    size=src['output_canvas_mm'];w,h=size
    frame=[np.array([[-1,-1],[w+1,-1],[w+1,h+1],[-1,h+1],[-1,-1]],float)]
    frame_text=gcode(map_native(frame,size,cfg),cfg,air=True)
    _,frame_check=parse_and_validate(frame_text,cfg,air=True)
    out.mkdir(parents=True)
    (out/'02_FULL_LABEL_OR_ART_CALIBRATE_FIRST.gcode').write_text(full,encoding='ascii',newline='\n')
    (out/'00_AIR_FRAME_CALIBRATE_FIRST.gcode').write_text(frame_text,encoding='ascii',newline='\n')
    Image.fromarray(grey).save(out/'SOURCE_CROP.png',dpi=(cfg.dpi,cfg.dpi))
    Image.fromarray(np.where(mask,0,255).astype(np.uint8)).save(out/'BINARY_TARGET.png',dpi=(cfg.dpi,cfg.dpi))
    sim.save(out/'SIMULATED_INK.png',dpi=(cfg.dpi,cfg.dpi))
    save_svg(out/'PEN_PATHS.svg',local,size,cfg.pen_mm)
    write_json(out/'TOOLPATHS.json',{'schema':'penplot.toolpaths.v1','coordinate_system':'local mm, top-left; +X right; +Y down',
        'canvas_mm':size,'pen_mm':cfg.pen_mm,'paths':[np.round(p,6).tolist() for p in local]})
    deps={name:importlib.metadata.version(name) for name in ['PyMuPDF','Pillow','numpy','opencv-python','scipy','scikit-image']}
    try:deps['pyzbar']=importlib.metadata.version('pyzbar')
    except importlib.metadata.PackageNotFoundError:pass
    manifest={'schema':'penplot.job.v1','engine_version':VERSION,'source':src,'settings':dataclasses.asdict(cfg),
        'engine_sha256':sha256(Path(__file__).read_bytes()),'dependencies':deps,'python':platform.python_version(),
        'coordinates':{'bed_mm':[256,256],'software_guard_mm':[20,236],
            'positioning':'measured pen-centred' if cfg.offset_x is not None else 'nozzle-centred; pen offset UNKNOWN',
            'transform':'X = center_x - dx + local_x - W/2; Y = center_y - dy + H/2 - local_y',
            'physical_swept_volume_verified':False},'path_generation':path_info,'gcode_checks':checks,
        'air_frame_checks':frame_check,'quality':quality,
        'status':'DIGITALLY_CHECKED_CALIBRATION_DEPENDENT_NOT_PHYSICALLY_VALIDATED',
        'steps':['bounded page/image render','ink bounding crop with white margin','native PDF scale or explicit image size',
            'binary threshold','inset contours for filled regions','skeleton for narrow features',
            'nearest-endpoint ordering without drawn travel','native-coordinate mapping','export restricted G-code',
            'independent reparse and geometry check','simulate pen footprint from exported G-code','compare decoded symbols'],
        'limitations':['Raster fallback, not native vector extraction; anti-aliased edges are quantized.',
            'Auto-crop removes outer blank margins and adds a fixed white margin; inspect code quiet zones.',
            'Decoder comparison checks detected symbols only, not a complete code inventory or an ISO print grade.',
            'No grayscale halftoning, OCR, ANSI/Unicode terminal rendering, multicolour passes or printer sender.',
            'Only full-size A1 native coordinates are implemented.'],
        'privacy':'Source, previews, payload bytes and toolpaths can contain personal information; keep private.'}
    write_json(out/'MANIFEST.json',manifest)
    with (out/'CONVERSION_TRACE.jsonl').open('w',encoding='utf-8',newline='\n') as f:
        for i,name in enumerate(manifest['steps'],1):
            f.write(json.dumps({'step':i,'operation':name,'job_source_sha256':src['sha256']},ensure_ascii=False)+'\n')
    refresh_checksums(out)
    return manifest


def refresh_checksums(out:Path)->None:
    files=sorted(p for p in out.iterdir() if p.is_file() and p.name!='SHA256SUMS.txt')
    (out/'SHA256SUMS.txt').write_text(''.join(f'{sha256(p.read_bytes())}  {p.name}\n' for p in files),encoding='utf-8',newline='\n')


def main()->None:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input',type=Path);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--width-mm',type=float,help='Ink width; omitted for original PDF physical size. Required for images/ASCII.')
    ap.add_argument('--pen-mm',type=float,required=True,help='MEASURED ink-line width, not pen barrel diameter.')
    ap.add_argument('--z-down',type=float,required=True,help='Calibrated NATIVE nozzle coordinate at light paper contact.')
    ap.add_argument('--lift-mm',type=float,default=2);ap.add_argument('--page',type=int,default=1)
    ap.add_argument('--center-x',type=float,default=128);ap.add_argument('--center-y',type=float,default=128)
    ap.add_argument('--offset-x',type=float);ap.add_argument('--offset-y',type=float)
    ap.add_argument('--dpi',type=int,default=600);ap.add_argument('--threshold',type=int,default=160)
    ap.add_argument('--mode',choices=['filled','outline'],default='filled')
    ap.add_argument('--require-symbols',action='store_true',help='Strict label mode: require source decoding and identical simulated payloads.')
    ap.add_argument('--ack-calibration',action='store_true',help='Acknowledge physical setup and unchanged execution requirements.')
    ns=ap.parse_args()
    if not ns.ack_calibration:ap.error('--ack-calibration is required. See README.md first.')
    try:
        cfg=Settings(dpi=ns.dpi,threshold=ns.threshold,pen_mm=ns.pen_mm,width_mm=ns.width_mm,page=ns.page,
            z_down=ns.z_down,lift_mm=ns.lift_mm,center_x=ns.center_x,center_y=ns.center_y,
            offset_x=ns.offset_x,offset_y=ns.offset_y,mode=ns.mode,require_symbols=ns.require_symbols)
        manifest=convert(ns.input,ns.out,cfg)
        print(json.dumps({'output':str(ns.out),'status':manifest['status'],
            'strokes':manifest['gcode_checks']['pen_down_strokes'],
            'nominal_minutes':manifest['gcode_checks']['nominal_seconds_excluding_initial_positioning_acceleration_and_firmware']/60,
            'digital_symbol_check':manifest['quality']['decode_gate']},indent=2))
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr);sys.exit(1)

if __name__=='__main__':main()
