import numpy as np, subprocess, math, random, sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio_ffmpeg
W,H,FPS,DUR=1920,1080,24,5.0
N=int(FPS*DUR)
out=sys.argv[1]
random.seed(7)
# background
yy,xx=np.mgrid[0:H,0:W].astype(np.float32)
r=np.sqrt(((xx-W/2)/W)**2+((yy-H*0.42)/H)**2)
t=np.clip(r*1.6,0,1)[...,None]
c1=np.array([28,40,92],np.float32); c2=np.array([6,9,26],np.float32)
bg=(c1*(1-t)+c2*t)
LORA="/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf"
LORAI="/usr/share/fonts/truetype/google-fonts/Lora-Italic-Variable.ttf"
def font(p,s,w=None):
    f=ImageFont.truetype(p,s)
    try:
        if w: f.set_variation_by_axes([w])
    except Exception: pass
    return f
fT=font(LORA,170,600); fS=font(LORAI,54,400); fH=font(LORA,40,500)
def text_layer(txt,f,fill,cy):
    L=Image.new("RGBA",(W,H),(0,0,0,0)); d=ImageDraw.Draw(L)
    bb=d.textbbox((0,0),txt,font=f); w=bb[2]-bb[0]; h=bb[3]-bb[1]
    d.text(((W-w)/2-bb[0],cy-h/2-bb[1]),txt,font=f,fill=fill)
    return L
title=text_layer("Lumio",fT,(244,205,120,255),H*0.56)
glow=title.filter(ImageFilter.GaussianBlur(18))
sub=text_layer("мультфильмы про вашу семью",fS,(240,232,214,255),H*0.70)
han=text_layer("t.me/lumioRF",fH,(200,190,170,255),H*0.80)
flakes=[(random.uniform(0,W),random.uniform(-H,H),random.uniform(1.2,4.2),random.uniform(30,90),random.uniform(0,6.28)) for _ in range(260)]
def ease(x): x=min(max(x,0),1); return x*x*(3-2*x)
exe=imageio_ffmpeg.get_ffmpeg_exe()
p=subprocess.Popen([exe,"-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r",str(FPS),"-i","-",
  "-f","lavfi","-t",str(DUR),"-i","anullsrc=r=44100:cl=stereo",
  "-c:v","libx264","-preset","medium","-crf","17","-pix_fmt","yuv420p","-c:a","aac","-shortest","-movflags","+faststart",out],stdin=subprocess.PIPE)
for i in range(N):
    s=i/FPS
    img=Image.fromarray(bg.astype(np.uint8)).convert("RGBA")
    # snow
    sn=Image.new("RGBA",(W,H),(0,0,0,0)); d=ImageDraw.Draw(sn)
    for x,y,rad,sp,ph in flakes:
        yy_=(y+sp*s*1.5)%(H+40)-20; xx_=x+12*math.sin(s*0.9+ph)
        a=int(150*(rad/4.2)+60)
        d.ellipse([xx_-rad,yy_-rad,xx_+rad,yy_+rad],fill=(255,255,255,a))
    img=Image.alpha_composite(img,sn.filter(ImageFilter.GaussianBlur(0.8)))
    # golden ball
    bx,by=W/2,H*0.27+10*math.sin(s*1.6); R=58
    gl=Image.new("RGBA",(W,H),(0,0,0,0)); g=ImageDraw.Draw(gl)
    pulse=0.85+0.15*math.sin(s*3)
    g.ellipse([bx-R*3.2,by-R*3.2,bx+R*3.2,by+R*3.2],fill=(255,190,80,int(70*pulse)))
    gl=gl.filter(ImageFilter.GaussianBlur(60))
    img=Image.alpha_composite(img,gl)
    ball=Image.new("RGBA",(W,H),(0,0,0,0)); b=ImageDraw.Draw(ball)
    for k in range(R,0,-1):
        f=k/R; col=(int(160+95*(1-f)),int(105+110*(1-f)),int(30+120*(1-f)**2),255)
        ox=-R*0.3*(1-f); oy=-R*0.35*(1-f)
        b.ellipse([bx+ox-k,by+oy-k,bx+ox+k,by+oy+k],fill=col)
    b.ellipse([bx-R*0.45-10,by-R*0.5-7,bx-R*0.45+10,by-R*0.5+7],fill=(255,250,235,230))
    b.rectangle([bx-9,by-R-14,bx+9,by-R+2],fill=(200,160,80,255))
    img=Image.alpha_composite(img,ball)
    # texts
    for L,t0 in ((glow,0.5),(title,0.5),(sub,1.4),(han,2.1)):
        a=ease((s-t0)/0.9)
        if s>DUR-0.6: a*=ease((DUR-s)/0.6)
        if a>0:
            T=L.copy(); al=T.getchannel("A").point(lambda v:int(v*a*(0.55 if L is glow else 1)))
            T.putalpha(al); img=Image.alpha_composite(img,T)
    p.stdin.write(img.convert("RGB").tobytes())
p.stdin.close(); p.wait(); print("ok",p.returncode)
