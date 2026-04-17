#pragma once

// Forward declarations
struct ID3D11ShaderResourceView;

// LUT data for a single monitor
struct lutData
{
	int left;
	int top;
	int size;
	bool isHdr;
	ID3D11ShaderResourceView* textureView;
	float* rawLut;
};

// LUT index calculation
unsigned int lut_index(unsigned int b, unsigned int g, unsigned int r, unsigned int c, unsigned int lut_size);

#define LUT_ACCESS_INDEX(lut, b, g, r, c, lut_size) (*((float*)(lut) + lut_index(b, g, r, c, lut_size)))

// LUT state (extern — defined in hook_lut.cpp)
extern int numLuts;
extern lutData* luts;

// Active-LUT target set: fixed-size array so Set/Unset/IsLUTActive can't race
// via realloc if DWM ever calls Present-hooks on multiple threads concurrently.
#define MAX_LUT_TARGETS 64
extern int numLutTargets;
extern void* lutTargets[MAX_LUT_TARGETS];

// LUT management functions
bool ParseLUT(lutData* lut, char* filename);
bool AddLUTs(char* folder);
bool IsLUTActive(void* target);
void SetLUTActive(void* target);
void UnsetLUTActive(void* target);
lutData* GetLUTDataFromCOverlayContext(void* context, bool hdr);
