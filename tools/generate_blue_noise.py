#!/usr/bin/env python3
"""
Downloads a 64x64 blue noise texture from momentsingraphics.de (Christoph Peters)
and converts it to a C++ byte array for embedding in DesktopLUT.

Usage:
    python generate_blue_noise.py > blue_noise_data.h

Or to update main.cpp directly:
    python generate_blue_noise.py --update ../src/main.cpp
"""

import urllib.request
import io
import sys
import argparse
import re

def download_blue_noise():
    """Download 64x64 blue noise PNG from free-blue-noise-textures repository."""
    # Using the Calinou mirror of Christoph Peters' textures (CC0 license)
    # Use LDR (8-bit) version - HDR versions are 16-bit and don't decode correctly
    url = "https://raw.githubusercontent.com/Calinou/free-blue-noise-textures/master/64_64/LDR_LLL1_0.png"

    print(f"Downloading from {url}...", file=sys.stderr)

    with urllib.request.urlopen(url) as response:
        png_data = response.read()

    return png_data

def decode_png(png_data):
    """Decode PNG to raw pixel values. Returns list of 4096 byte values."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_data))
        img = img.convert('L')  # Convert to grayscale

        if img.size != (64, 64):
            print(f"Warning: Image size is {img.size}, expected (64, 64). Resizing...", file=sys.stderr)
            img = img.resize((64, 64), Image.Resampling.NEAREST)

        pixels = list(img.getdata())
        return pixels
    except ImportError:
        print("PIL/Pillow not found. Install with: pip install Pillow", file=sys.stderr)
        sys.exit(1)

def format_cpp_array(pixels, name="g_blueNoiseData"):
    """Format pixel data as a C++ array."""
    lines = []
    lines.append(f"// 64x64 blue noise texture data (single channel, 8-bit)")
    lines.append(f"// Source: momentsingraphics.de (Christoph Peters) - CC0 Public Domain")
    lines.append(f"// Downloaded from free-blue-noise-textures repository")
    lines.append(f"static const unsigned char {name}[64 * 64] = {{")

    # Format 16 values per line for readability
    for row in range(64):
        row_values = pixels[row * 64 : (row + 1) * 64]
        # Split into chunks of 16
        for chunk_start in range(0, 64, 16):
            chunk = row_values[chunk_start:chunk_start + 16]
            formatted = ",".join(f"{v:3d}" for v in chunk)
            if row == 63 and chunk_start == 48:
                lines.append(f"    {formatted}")  # Last line, no trailing comma
            else:
                lines.append(f"    {formatted},")

    lines.append("};")
    return "\n".join(lines)

def update_main_cpp(filepath, new_array_code):
    """Replace the existing blue noise array in main.cpp."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Pattern to match the existing array declaration
    pattern = r'// 64x64 blue noise texture data.*?static const unsigned char g_blueNoiseData\[64 \* 64\] = \{[^}]+\};'

    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print("Could not find existing g_blueNoiseData array in file!", file=sys.stderr)
        sys.exit(1)

    new_content = content[:match.start()] + new_array_code + content[match.end():]

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"Updated {filepath} with new blue noise data.", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Generate blue noise C++ array from momentsingraphics.de")
    parser.add_argument('--update', metavar='FILE', help='Update main.cpp file in place')
    args = parser.parse_args()

    # Download and decode
    png_data = download_blue_noise()
    pixels = decode_png(png_data)

    # Verify we have correct amount of data
    if len(pixels) != 64 * 64:
        print(f"Error: Expected 4096 pixels, got {len(pixels)}", file=sys.stderr)
        sys.exit(1)

    # Format as C++ array
    cpp_code = format_cpp_array(pixels)

    if args.update:
        update_main_cpp(args.update, cpp_code)
    else:
        print(cpp_code)

if __name__ == "__main__":
    main()
