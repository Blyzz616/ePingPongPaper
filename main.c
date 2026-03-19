#include <stdio.h>
#include <stdlib.h>
#include "IT8951.h"

/*
 * IT8951 e-paper display binary for ping-pong scorer.
 *
 * Usage:
 *   sudo ./IT8951 <x> <y> <image.bmp> [mode]
 *
 * mode values:
 *   2 = GC16  — full quality, ~4s   (default if omitted)
 *   4 = A2    — fast binary, ~0.3s  (use for in-game partial updates)
 *
 * Examples:
 *   sudo ./IT8951 0   0   /home/jim/images/gamelen.bmp        # full GC16
 *   sudo ./IT8951 35  218 /home/jim/images/7.bmp           4  # fast A2
 */

int main(int argc, char *argv[])
{
    if (argc < 4 || argc > 5)
    {
        printf("Usage: %s <x> <y> <image.bmp> [mode]\n", argv[0]);
        printf("  mode 2 = GC16 full refresh (default)\n");
        printf("  mode 4 = A2 fast partial refresh\n");
        return 1;
    }

    if (IT8951_Init())
    {
        printf("IT8951_Init error\n");
        return 1;
    }

    uint32_t x, y, mode;
    sscanf(argv[1], "%d", &x);
    sscanf(argv[2], "%d", &y);
    mode = (argc == 5) ? (uint32_t)atoi(argv[4]) : 2;

    IT8951_BMP_Example(x, y, argv[3], mode);

    IT8951_Cancel();
    return 0;
}
