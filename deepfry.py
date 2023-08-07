#!/usr/bin/env python3

from PIL import Image, ImageEnhance, ImageFilter
from tqdm import tqdm
from pathlib import Path
import glob

INPUT_PATH="./archive/memes/memes"
OUTPUT_PATH="./out"

CONTRAST = 2
SHARPNESS = 4
BRIGHTNESS = 1.3
COLOR = 3
JPEG_QUALITY = 2
JPEG_ITERATIONS = 20

def deepfry(infile, outfile):
    im = Image.open(infile)

    if im.mode in ("RGBA", "P"):
        im = im.convert("RGB")
    
    enh = ImageEnhance.Contrast(im)
    im = enh.enhance(CONTRAST)
    
    enh = ImageEnhance.Sharpness(im)
    im = enh.enhance(SHARPNESS)

    enh = ImageEnhance.Brightness(im)
    im = enh.enhance(BRIGHTNESS)

    enh = ImageEnhance.Color(im)
    im = enh.enhance(COLOR)
    
    for i in range(1, JPEG_ITERATIONS):
        im.save(outfile, "JPEG", optimize=True, quality=JPEG_QUALITY)
        im = Image.open(outfile)

for path in tqdm(glob.glob("{INPUT_PATH}/*")):
    path = Path(path)
    deepfry(path, f"{OUTPUT_PATH}/{path.name}")
