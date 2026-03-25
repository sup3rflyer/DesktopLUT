#include "pch.h"
#include "hook_lut.h"
#include "hook_log.h"
#include "hook_render.h"
#include <cmath>

extern bool isWindows11_25h2;

int numLuts = 0;
lutData* luts = NULL;
int numLutTargets = 0;
void** lutTargets = NULL;

unsigned int lut_index(const unsigned int b, const unsigned int g, const unsigned int r, const unsigned int c,
                       const unsigned int lut_size)
{
	return lut_size * lut_size * 4 * b + lut_size * 4 * g + 4 * r + c;
}

bool ParseLUT(lutData* lut, char* filename)
{
	FILE* file = fopen(filename, "r");
	if (file == NULL) return false;

	char line[256];
	unsigned int lutSize;

	while (1)
	{
		if (!fgets(line, sizeof(line), file))
		{
			fclose(file);
			return false;
		}
		if (sscanf(line, "LUT_3D_SIZE %d", &lutSize) == 1)
		{
			if (lutSize < 2 || lutSize > 128)
			{
				fclose(file);
				return false;
			}
			break;
		}
	}

	float* rawLut = (float*)malloc((size_t)lutSize * lutSize * lutSize * 4 * sizeof(float));
	if (!rawLut)
	{
		fclose(file);
		return false;
	}


	for (unsigned int b = 0; b < lutSize; b++)
	{
		for (unsigned int g = 0; g < lutSize; g++)
		{
			for (unsigned int r = 0; r < lutSize; r++)
			{
				while (1)
				{
					if (!fgets(line, sizeof(line), file))
					{
						fclose(file);
						free(rawLut);
						return false;
					}
					if (((line[0] >= '0' && line[0] <= '9') || line[0] == '-' || line[0] == '+' || line[0] == '.') && line[0] != '#' && line[0] != '\n')
					{
						float red, green, blue;

						if (sscanf(line, "%f%f%f", &red, &green, &blue) != 3)
						{
							fclose(file);
							free(rawLut);
							return false;
						}
						if (!std::isfinite(red) || !std::isfinite(green) || !std::isfinite(blue))
						{
							fclose(file);
							free(rawLut);
							return false;
						}
						LUT_ACCESS_INDEX(rawLut, b, g, r, 0, lutSize) = red;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 1, lutSize) = green;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 2, lutSize) = blue;
						LUT_ACCESS_INDEX(rawLut, b, g, r, 3, lutSize) = 1;

						break;
					}
				}
			}
		}
	}
	fclose(file);
	lut->size = lutSize;
	lut->rawLut = rawLut;
	return true;
}

bool AddLUTs(char* folder)
{
	WIN32_FIND_DATAA findData;

	char path[MAX_PATH];
	snprintf(path, sizeof(path), "%s\\*", folder);
	HANDLE hFind = FindFirstFileA(path, &findData);
	if (hFind == INVALID_HANDLE_VALUE) return false;
	do
	{
		if (!(findData.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
		{
			char filePath[MAX_PATH];
			char* fileName = findData.cFileName;

			snprintf(filePath, sizeof(filePath), "%s\\%s", folder, fileName);

			lutData* tmp = (lutData*)realloc(luts, (size_t)(numLuts + 1) * sizeof(lutData));
			if (!tmp)
			{
				FindClose(hFind);
				return false;
			}
			luts = tmp;
			lutData* lut = &luts[numLuts];
			if (sscanf(findData.cFileName, "%d_%d", &lut->left, &lut->top) == 2)
			{
				lut->isHdr = strstr(fileName, "hdr") != NULL;
				lut->textureView = NULL;
				if (!ParseLUT(lut, filePath))
				{
					char warnMsg[MAX_PATH + 64];
					snprintf(warnMsg, sizeof(warnMsg), "WARNING: Skipping unparseable LUT: %s", filePath);
					log_to_file(warnMsg);
					continue;
				}
				numLuts++;
			}
		}
	}
	while (FindNextFileA(hFind, &findData) != 0);
	FindClose(hFind);
	return true;
}

bool IsLUTActive(void* target)
{
	for (int i = 0; i < numLutTargets; i++)
	{
		if (lutTargets[i] == target)
		{
			return true;
		}
	}
	return false;
}

void SetLUTActive(void* target)
{
	if (!IsLUTActive(target))
	{
		void** tmp = (void**)realloc(lutTargets, (size_t)(numLutTargets + 1) * sizeof(void*));
		if (!tmp) return;
		lutTargets = tmp;
		lutTargets[numLutTargets++] = target;
	}
}

void UnsetLUTActive(void* target)
{
	for (int i = 0; i < numLutTargets; i++)
	{
		if (lutTargets[i] == target)
		{
			lutTargets[i] = lutTargets[--numLutTargets];
			if (numLutTargets > 0)
			{
				void** tmp = (void**)realloc(lutTargets, (size_t)numLutTargets * sizeof(void*));
				if (tmp) lutTargets = tmp;
			}
			else
			{
				free(lutTargets);
				lutTargets = NULL;
			}
			return;
		}
	}
}

lutData* GetLUTDataFromCOverlayContext(void* context, bool hdr)
{
	int left, top;
	GetMonitorPositionFromContext(context, left, top);

	for (int i = 0; i < numLuts; i++)
	{
		if (luts[i].left == left && luts[i].top == top && luts[i].isHdr == hdr)
		{
			return &luts[i];
		}
	}

	// No LUT staged for this monitor position — skip LUT application
	return NULL;
}
