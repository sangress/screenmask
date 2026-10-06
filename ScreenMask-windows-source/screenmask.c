/*
 * Screen Mask for Windows - cover areas of the screen with black rectangles.
 *
 *   pitch black   A solid black, always-on-top rectangle. Nobody sees what is
 *                 underneath, including you.
 *
 *   zoom block    You keep seeing the content. Anything that captures the
 *                 screen (Zoom, Meet, Teams, screenshots, recorders) gets a
 *                 black rectangle instead. This uses a Windows feature: an
 *                 almost fully transparent window on top of the area, marked
 *                 with SetWindowDisplayAffinity(WDA_MONITOR), which Windows
 *                 shows "with no content" - black - everywhere except on the
 *                 monitor itself.
 *
 * The program checks its own work: it captures a few pixels inside every
 * zoom-block area the same way a screen-sharing app would, and warns loudly
 * if they are not black.
 *
 * Build (MinGW-w64):
 *   x86_64-w64-mingw32-windres screenmask.rc -O coff -o screenmask.res
 *   x86_64-w64-mingw32-gcc -O2 -municode -mwindows -static \
 *       screenmask.c screenmask.res -o ScreenMask.exe -lcomctl32 -lgdi32 -lshell32 -ldwmapi
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WINVER 0x0A00
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <shlobj.h>
#include <dwmapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#ifndef WDA_NONE
#define WDA_NONE 0x00000000
#endif
#ifndef WDA_MONITOR
#define WDA_MONITOR 0x00000001
#endif
#ifndef DWMWA_CLOAKED
#define DWMWA_CLOAKED 14
#endif

#define APP_NAME    L"Screen Mask"
#define CLS_PANEL   L"ScreenMaskPanel"
#define CLS_AREA    L"ScreenMaskArea"
#define CLS_OUTLINE L"ScreenMaskOutline"
#define CLS_DRAW    L"ScreenMaskDraw"

enum { PITCH = 0, ZOOM = 1 };
enum {
    ID_ADD_PITCH = 101, ID_ADD_ZOOM, ID_LIST, ID_SWITCH, ID_REMOVE, ID_EDIT,
    ID_OUTLINE, ID_STATUS,
    IDM_SWITCH = 201, IDM_REMOVE, IDM_EDIT, IDM_OPEN
};
enum { TIMER_TOPMOST = 1, TIMER_CHECK, TIMER_CHECK_SOON };
enum { CHECK_UNSET = -2, CHECK_NONE = -1, CHECK_FAIL = 0, CHECK_OK = 1, CHECK_UNKNOWN = 2 };

#define MAX_AREAS 64
#define MIN_SIZE  16
#define AMBER     RGB(245, 166, 35)

#define TXT_NONE L"Add a zoom-block area and this line tells you whether screen " \
                 L"capture is really being blocked."
#define TXT_OK   L"Zoom block check passed: screen capture shows these areas black."
#define TXT_UNKNOWN L"Zoom block could not be checked just now: another window is on top " \
                    L"of a zoom-block area, or the area is off screen. It is checked again " \
                    L"every few seconds."
#define TXT_FAIL L"WARNING: zoom block is NOT working on this PC. Screen capture " \
                 L"still shows what is under the zoom-block areas. Do not rely on it."

typedef struct {
    int  mode;
    RECT rc;            /* screen coordinates, physical pixels */
    HWND block;         /* the rectangle itself */
    HWND outline;       /* thin frame shown around a zoom-block area */
    BOOL affinity_ok;   /* Windows accepted the capture protection */
} Area;

static HINSTANCE g_inst;
static int   g_dpi = 96;
static HFONT g_font, g_bold;
static HWND  g_panel, g_list, g_edit_box, g_outline_box, g_status, g_draw;
static Area  g_areas[MAX_AREAS];
static int   g_count;
static BOOL  g_editing, g_outlines = TRUE, g_in_menu, g_warned;
static int   g_check = CHECK_UNSET, g_fail_streak;
static int   g_draw_mode;
static BOOL  g_draw_active;
static POINT g_draw_from, g_draw_to;
static struct { HWND hwnd; int kind; POINT start; RECT rc; } g_drag;

static void apply_area(Area *a);
static void refresh_list(void);
static void save_config(void);
static void schedule_check(void);

static int S(int v) { return MulDiv(v, g_dpi, 96); }
static int ring(void) { int r = S(3); return r < 2 ? 2 : r; }

static int area_index(HWND h)
{
    for (int i = 0; i < g_count; i++)
        if (g_areas[i].block == h || g_areas[i].outline == h) return i;
    return -1;
}

/* ------------------------------------------------------------------ config */

static BOOL config_path(wchar_t *path, BOOL make_dir)
{
    wchar_t base[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_APPDATA, NULL, 0, base))) return FALSE;
    swprintf(path, MAX_PATH, L"%ls\\ScreenMask", base);
    if (make_dir) CreateDirectoryW(path, NULL);
    wcscat(path, L"\\config.txt");
    return TRUE;
}

static void save_config(void)
{
    wchar_t path[MAX_PATH];
    if (!config_path(path, TRUE)) return;
    FILE *f = _wfopen(path, L"w");
    if (!f) return;
    fwprintf(f, L"outlines=%d\n", g_outlines ? 1 : 0);
    for (int i = 0; i < g_count; i++) {
        const RECT *r = &g_areas[i].rc;
        fwprintf(f, L"area=%ls %ld %ld %ld %ld\n",
                 g_areas[i].mode == PITCH ? L"pitch" : L"zoom",
                 r->left, r->top, r->right - r->left, r->bottom - r->top);
    }
    fclose(f);
}

static HWND create_block(void)
{
    return CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
        CLS_AREA, L"Screen Mask area", WS_POPUP, 0, 0, 10, 10, NULL, NULL, g_inst, NULL);
}

static Area *add_area(int mode, int x, int y, int w, int h)
{
    if (g_count >= MAX_AREAS) return NULL;
    if (w < MIN_SIZE) w = MIN_SIZE;
    if (h < MIN_SIZE) h = MIN_SIZE;
    Area *a = &g_areas[g_count];
    ZeroMemory(a, sizeof *a);
    a->mode = mode == ZOOM ? ZOOM : PITCH;
    SetRect(&a->rc, x, y, x + w, y + h);
    a->block = create_block();
    if (!a->block) return NULL;
    g_count++;
    apply_area(a);
    return a;
}

static void load_config(void)
{
    wchar_t path[MAX_PATH], line[256], kind[32];
    if (!config_path(path, FALSE)) return;
    FILE *f = _wfopen(path, L"r");
    if (!f) return;
    while (fgetws(line, 256, f)) {
        int v;
        long x, y, w, h;
        if (swscanf(line, L"outlines=%d", &v) == 1) {
            g_outlines = v != 0;
        } else if (swscanf(line, L"area=%31ls %ld %ld %ld %ld", kind, &x, &y, &w, &h) == 5) {
            add_area(wcscmp(kind, L"zoom") == 0 ? ZOOM : PITCH, x, y, w, h);
        }
    }
    fclose(f);
}

/* -------------------------------------------------------------- the areas */

static void set_outline_region(Area *a)
{
    int w = a->rc.right - a->rc.left, h = a->rc.bottom - a->rc.top, t = ring();
    HRGN outer = CreateRectRgn(0, 0, w, h);
    if (w > 2 * t && h > 2 * t) {
        HRGN inner = CreateRectRgn(t, t, w - t, h - t);
        CombineRgn(outer, outer, inner, RGN_DIFF);
        DeleteObject(inner);
    }
    SetWindowRgn(a->outline, outer, TRUE);   /* the system now owns the region */
}

/* Bring one area's windows in line with its mode and the edit state. */
static void apply_area(Area *a)
{
    LONG_PTR ex = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED;
    BYTE alpha = 255;
    DWORD affinity = WDA_NONE;
    int w = a->rc.right - a->rc.left, h = a->rc.bottom - a->rc.top;

    if (a->mode == ZOOM) {
        affinity = WDA_MONITOR;
        if (g_editing) {
            alpha = 120;                 /* see-through amber, can be grabbed */
        } else {
            alpha = 1;                   /* invisible to you, but still a window */
            ex |= WS_EX_TRANSPARENT;     /* mouse clicks go to what is underneath */
        }
    }
    SetWindowLongPtrW(a->block, GWL_EXSTYLE, ex);
    SetLayeredWindowAttributes(a->block, 0, alpha, LWA_ALPHA);
    a->affinity_ok = SetWindowDisplayAffinity(a->block, affinity) != 0;
    SetWindowPos(a->block, HWND_TOPMOST, a->rc.left, a->rc.top, w, h,
                 SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW);
    InvalidateRect(a->block, NULL, TRUE);

    if (a->mode == ZOOM && !g_editing && g_outlines) {
        if (!a->outline) {
            a->outline = CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED |
                WS_EX_TRANSPARENT,
                CLS_OUTLINE, L"Screen Mask outline", WS_POPUP, 0, 0, 10, 10,
                NULL, NULL, g_inst, NULL);
            if (a->outline) {
                SetLayeredWindowAttributes(a->outline, 0, 255, LWA_ALPHA);
                SetWindowDisplayAffinity(a->outline, WDA_MONITOR);
            }
        }
        if (a->outline) {
            SetWindowPos(a->outline, HWND_TOPMOST, a->rc.left, a->rc.top, w, h,
                         SWP_NOACTIVATE | SWP_SHOWWINDOW);
            set_outline_region(a);
            InvalidateRect(a->outline, NULL, TRUE);
        }
    } else if (a->outline) {
        DestroyWindow(a->outline);
        a->outline = NULL;
    }
}

static void apply_all(void)
{
    for (int i = 0; i < g_count; i++) apply_area(&g_areas[i]);
}

static void remove_area(int i)
{
    if (i < 0 || i >= g_count) return;
    HWND block = g_areas[i].block, outline = g_areas[i].outline;
    for (int k = i; k < g_count - 1; k++) g_areas[k] = g_areas[k + 1];
    g_count--;
    if (outline) DestroyWindow(outline);
    if (block) DestroyWindow(block);
    refresh_list();
    save_config();
    schedule_check();
}

static void switch_area(int i)
{
    if (i < 0 || i >= g_count) return;
    g_areas[i].mode = g_areas[i].mode == PITCH ? ZOOM : PITCH;
    apply_area(&g_areas[i]);
    refresh_list();
    SendMessageW(g_list, LB_SETCURSEL, i, 0);
    save_config();
    schedule_check();
}

static void set_editing(BOOL on)
{
    g_editing = on;
    if (g_edit_box) Button_SetCheck(g_edit_box, on ? BST_CHECKED : BST_UNCHECKED);
    apply_all();
    schedule_check();
}

static void draw_tag(HDC dc, const RECT *client, const wchar_t *text, COLORREF back)
{
    if (client->right < S(110) || client->bottom < S(40)) return;
    HFONT old = SelectObject(dc, g_bold);
    SIZE size;
    GetTextExtentPoint32W(dc, text, (int)wcslen(text), &size);
    RECT tag = { S(8), S(8), S(8) + size.cx + S(12), S(8) + size.cy + S(6) };
    if (tag.right > client->right - S(8)) tag.right = client->right - S(8);
    HBRUSH brush = CreateSolidBrush(back);
    FillRect(dc, &tag, brush);
    DeleteObject(brush);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(0, 0, 0));
    RECT text_rc = { tag.left + S(6), tag.top + S(3), tag.right - S(2), tag.bottom };
    DrawTextW(dc, text, -1, &text_rc, DT_LEFT | DT_SINGLELINE | DT_END_ELLIPSIS | DT_NOPREFIX);
    SelectObject(dc, old);
}

static void draw_frame(HDC dc, const RECT *client, COLORREF color)
{
    LOGBRUSH lb = { BS_SOLID, color, 0 };
    HPEN pen = ExtCreatePen(PS_GEOMETRIC | PS_DASH | PS_ENDCAP_FLAT, S(2), &lb, 0, NULL);
    HPEN old_pen = SelectObject(dc, pen);
    HBRUSH old_brush = SelectObject(dc, GetStockObject(NULL_BRUSH));
    Rectangle(dc, S(1), S(1), client->right - S(1) + 1, client->bottom - S(1) + 1);
    SelectObject(dc, old_brush);
    SelectObject(dc, old_pen);
    DeleteObject(pen);
}

static void paint_area(HWND hwnd)
{
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(hwnd, &ps);
    RECT client;
    GetClientRect(hwnd, &client);
    int i = area_index(hwnd);
    int mode = i >= 0 ? g_areas[i].mode : PITCH;

    if (mode == ZOOM && g_editing) {
        HBRUSH brush = CreateSolidBrush(AMBER);
        FillRect(dc, &client, brush);
        DeleteObject(brush);
        draw_frame(dc, &client, RGB(120, 70, 0));
        draw_tag(dc, &client, L"Zoom block - hidden from viewers", RGB(255, 255, 255));
    } else {
        FillRect(dc, &client, GetStockObject(BLACK_BRUSH));
        if (mode == PITCH && g_editing) {
            draw_frame(dc, &client, RGB(150, 150, 150));
            draw_tag(dc, &client, L"Pitch black", RGB(150, 150, 150));
        }
    }
    EndPaint(hwnd, &ps);
}

enum { HIT_MOVE = 0, HIT_L = 1, HIT_R = 2, HIT_T = 4, HIT_B = 8 };

static int hit_test(HWND hwnd, POINT client_pt)
{
    RECT c;
    GetClientRect(hwnd, &c);
    int e = S(8);
    if (e > c.right / 3) e = c.right / 3;
    if (e > c.bottom / 3) e = c.bottom / 3;
    if (e < 3) e = 3;
    int hit = HIT_MOVE;
    if (client_pt.x < e) hit |= HIT_L; else if (client_pt.x >= c.right - e) hit |= HIT_R;
    if (client_pt.y < e) hit |= HIT_T; else if (client_pt.y >= c.bottom - e) hit |= HIT_B;
    return hit;
}

static LPCWSTR cursor_for(int hit)
{
    switch (hit) {
    case HIT_L: case HIT_R: return IDC_SIZEWE;
    case HIT_T: case HIT_B: return IDC_SIZENS;
    case HIT_L | HIT_T: case HIT_R | HIT_B: return IDC_SIZENWSE;
    case HIT_R | HIT_T: case HIT_L | HIT_B: return IDC_SIZENESW;
    default: return IDC_SIZEALL;
    }
}

static void drag_to(POINT now)
{
    int i = area_index(g_drag.hwnd);
    if (i < 0) return;
    int dx = now.x - g_drag.start.x, dy = now.y - g_drag.start.y;
    RECT r = g_drag.rc;
    if (g_drag.kind == HIT_MOVE) {
        OffsetRect(&r, dx, dy);
    } else {
        if (g_drag.kind & HIT_L) { r.left += dx; if (r.left > r.right - MIN_SIZE) r.left = r.right - MIN_SIZE; }
        if (g_drag.kind & HIT_R) { r.right += dx; if (r.right < r.left + MIN_SIZE) r.right = r.left + MIN_SIZE; }
        if (g_drag.kind & HIT_T) { r.top += dy; if (r.top > r.bottom - MIN_SIZE) r.top = r.bottom - MIN_SIZE; }
        if (g_drag.kind & HIT_B) { r.bottom += dy; if (r.bottom < r.top + MIN_SIZE) r.bottom = r.top + MIN_SIZE; }
    }
    /* keep a corner of it on the desktop */
    int vx = GetSystemMetrics(SM_XVIRTUALSCREEN), vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
    int vw = GetSystemMetrics(SM_CXVIRTUALSCREEN), vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    int keep = S(24), ox = 0, oy = 0;
    if (r.right < vx + keep) ox = vx + keep - r.right;
    if (r.left > vx + vw - keep) ox = vx + vw - keep - r.left;
    if (r.bottom < vy + keep) oy = vy + keep - r.bottom;
    if (r.top > vy + vh - keep) oy = vy + vh - keep - r.top;
    OffsetRect(&r, ox, oy);

    g_areas[i].rc = r;
    SetWindowPos(g_drag.hwnd, NULL, r.left, r.top, r.right - r.left, r.bottom - r.top,
                 SWP_NOACTIVATE | SWP_NOZORDER);
    InvalidateRect(g_drag.hwnd, NULL, TRUE);
    refresh_list();
}

static void end_drag(void)
{
    if (!g_drag.hwnd) return;
    g_drag.hwnd = NULL;
    save_config();
    schedule_check();
}

static void area_menu(HWND hwnd)
{
    int i = area_index(hwnd);
    if (i < 0) return;
    HMENU menu = CreatePopupMenu();
    AppendMenuW(menu, MF_STRING, IDM_SWITCH,
                g_areas[i].mode == PITCH ? L"Switch to zoom block" : L"Switch to pitch black");
    AppendMenuW(menu, MF_STRING | (g_editing ? MF_CHECKED : 0), IDM_EDIT,
                L"Edit areas (move / resize)");
    AppendMenuW(menu, MF_STRING, IDM_REMOVE, L"Remove this area");
    AppendMenuW(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuW(menu, MF_STRING, IDM_OPEN, L"Open Screen Mask");
    POINT p;
    GetCursorPos(&p);
    g_in_menu = TRUE;
    SetForegroundWindow(hwnd);
    int cmd = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                             p.x, p.y, 0, hwnd, NULL);
    g_in_menu = FALSE;
    DestroyMenu(menu);
    PostMessageW(hwnd, WM_NULL, 0, 0);

    i = area_index(hwnd);
    switch (cmd) {
    case IDM_SWITCH: switch_area(i); break;
    case IDM_REMOVE: remove_area(i); break;
    case IDM_EDIT:   set_editing(!g_editing); break;
    case IDM_OPEN:
        ShowWindow(g_panel, IsIconic(g_panel) ? SW_RESTORE : SW_SHOW);
        SetForegroundWindow(g_panel);
        break;
    }
}

static LRESULT CALLBACK AreaProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT:
        paint_area(hwnd);
        return 0;
    case WM_MOUSEACTIVATE:
        return MA_NOACTIVATE;
    case WM_SETCURSOR:
        if (g_editing && LOWORD(lp) == HTCLIENT) {
            POINT p;
            GetCursorPos(&p);
            ScreenToClient(hwnd, &p);
            SetCursor(LoadCursorW(NULL, cursor_for(hit_test(hwnd, p))));
            return TRUE;
        }
        break;
    case WM_LBUTTONDOWN:
        if (g_editing) {
            int i = area_index(hwnd);
            if (i >= 0) {
                POINT p = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
                g_drag.hwnd = hwnd;
                g_drag.kind = hit_test(hwnd, p);
                g_drag.rc = g_areas[i].rc;
                GetCursorPos(&g_drag.start);
                SetCapture(hwnd);
            }
        }
        return 0;
    case WM_MOUSEMOVE:
        if (g_drag.hwnd == hwnd && GetCapture() == hwnd) {
            POINT now;
            GetCursorPos(&now);
            drag_to(now);
        }
        return 0;
    case WM_LBUTTONUP:
        if (g_drag.hwnd == hwnd) ReleaseCapture();
        return 0;
    case WM_CAPTURECHANGED:
        if (g_drag.hwnd == hwnd) end_drag();
        return 0;
    case WM_RBUTTONUP:
        area_menu(hwnd);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static LRESULT CALLBACK OutlineProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_MOUSEACTIVATE:
        return MA_NOACTIVATE;
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(hwnd, &ps);
        RECT client;
        GetClientRect(hwnd, &client);
        HBRUSH brush = CreateSolidBrush(AMBER);
        FillRect(dc, &client, brush);
        DeleteObject(brush);
        /* black dashes on amber: visible on any background */
        int t = ring();
        LOGBRUSH lb = { BS_SOLID, RGB(0, 0, 0), 0 };
        DWORD dashes[2] = { (DWORD)S(8), (DWORD)S(8) };
        HPEN pen = ExtCreatePen(PS_GEOMETRIC | PS_USERSTYLE | PS_ENDCAP_FLAT, t, &lb, 2, dashes);
        HPEN old_pen = SelectObject(dc, pen);
        HBRUSH old_brush = SelectObject(dc, GetStockObject(NULL_BRUSH));
        Rectangle(dc, t / 2, t / 2, client.right - t / 2, client.bottom - t / 2);
        SelectObject(dc, old_brush);
        SelectObject(dc, old_pen);
        DeleteObject(pen);
        EndPaint(hwnd, &ps);
        return 0;
    }
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

/* ------------------------------------------------- drawing a new rectangle */

static RECT draw_rect(void)
{
    RECT r;
    r.left = min(g_draw_from.x, g_draw_to.x);
    r.top = min(g_draw_from.y, g_draw_to.y);
    r.right = max(g_draw_from.x, g_draw_to.x);
    r.bottom = max(g_draw_from.y, g_draw_to.y);
    return r;
}

static void end_drawing(void)
{
    if (g_draw) {
        HWND h = g_draw;
        g_draw = NULL;
        g_draw_active = FALSE;
        if (GetCapture() == h) ReleaseCapture();
        DestroyWindow(h);
    }
}

static void begin_drawing(int mode)
{
    if (g_draw) return;
    g_draw_mode = mode;
    g_draw_active = FALSE;
    g_draw = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED, CLS_DRAW,
        L"Screen Mask area selection", WS_POPUP,
        GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN),
        GetSystemMetrics(SM_CXVIRTUALSCREEN), GetSystemMetrics(SM_CYVIRTUALSCREEN),
        NULL, NULL, g_inst, NULL);
    if (!g_draw) return;
    SetLayeredWindowAttributes(g_draw, 0, 120, LWA_ALPHA);
    ShowWindow(g_draw, SW_SHOW);
    SetForegroundWindow(g_draw);          /* so Esc reaches it */
}

static LRESULT CALLBACK DrawProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_SETCURSOR:
        SetCursor(LoadCursorW(NULL, IDC_CROSS));
        return TRUE;
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(hwnd, &ps);
        RECT client;
        GetClientRect(hwnd, &client);
        FillRect(dc, &ps.rcPaint, GetStockObject(BLACK_BRUSH));
        if (g_draw_active) {
            RECT r = draw_rect();
            HBRUSH brush = CreateSolidBrush(g_draw_mode == ZOOM ? AMBER : RGB(110, 110, 110));
            FillRect(dc, &r, brush);
            DeleteObject(brush);
            FrameRect(dc, &r, GetStockObject(WHITE_BRUSH));
        }
        /* hint, centred near the top of the primary monitor */
        POINT origin = { 0, 0 };
        ScreenToClient(hwnd, &origin);
        const wchar_t *hint = g_draw_mode == ZOOM
            ? L"Drag to mark the zoom-block area.   Right-click or Esc to cancel."
            : L"Drag to mark the pitch-black area.   Right-click or Esc to cancel.";
        HFONT old = SelectObject(dc, g_bold);
        SetBkMode(dc, OPAQUE);
        SetBkColor(dc, RGB(0, 0, 0));
        SetTextColor(dc, RGB(255, 255, 255));
        RECT text = { origin.x, origin.y + S(40), origin.x + GetSystemMetrics(SM_CXSCREEN),
                      origin.y + S(80) };
        DrawTextW(dc, hint, -1, &text, DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
        SelectObject(dc, old);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_LBUTTONDOWN:
        g_draw_from.x = g_draw_to.x = GET_X_LPARAM(lp);
        g_draw_from.y = g_draw_to.y = GET_Y_LPARAM(lp);
        g_draw_active = TRUE;
        SetCapture(hwnd);
        return 0;
    case WM_MOUSEMOVE:
        if (g_draw_active) {
            g_draw_to.x = GET_X_LPARAM(lp);
            g_draw_to.y = GET_Y_LPARAM(lp);
            InvalidateRect(hwnd, NULL, FALSE);
        }
        return 0;
    case WM_LBUTTONUP:
        if (g_draw_active) {
            g_draw_to.x = GET_X_LPARAM(lp);
            g_draw_to.y = GET_Y_LPARAM(lp);
            RECT r = draw_rect();
            g_draw_active = FALSE;
            ReleaseCapture();
            if (r.right - r.left >= MIN_SIZE && r.bottom - r.top >= MIN_SIZE) {
                POINT tl = { r.left, r.top };
                ClientToScreen(hwnd, &tl);
                int mode = g_draw_mode;
                end_drawing();
                if (add_area(mode, tl.x, tl.y, r.right - r.left, r.bottom - r.top)) {
                    refresh_list();
                    save_config();
                    schedule_check();
                }
            } else {
                InvalidateRect(hwnd, NULL, FALSE);   /* a stray click: keep waiting */
            }
        }
        return 0;
    case WM_RBUTTONDOWN:
        end_drawing();
        return 0;
    case WM_KEYDOWN:
        if (wp == VK_ESCAPE) end_drawing();
        return 0;
    case WM_KILLFOCUS:
        /* nothing: the user may click elsewhere and come back */
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

/* ------------------------------------------------------ checking our work */

/* The top-most visible window at a point, going by stacking order. */
static HWND top_window_at(POINT pt)
{
    for (HWND w = GetTopWindow(NULL); w; w = GetWindow(w, GW_HWNDNEXT)) {
        RECT r;
        BOOL cloaked = FALSE;
        if (!IsWindowVisible(w) || IsIconic(w)) continue;
        if (!GetWindowRect(w, &r) || !PtInRect(&r, pt)) continue;
        if (SUCCEEDED(DwmGetWindowAttribute(w, DWMWA_CLOAKED, &cloaked, sizeof cloaked)) &&
            cloaked) continue;
        HRGN region = CreateRectRgn(0, 0, 0, 0);
        int kind = GetWindowRgn(w, region);
        BOOL inside = kind == ERROR || PtInRegion(region, pt.x - r.left, pt.y - r.top);
        DeleteObject(region);
        if (inside) return w;
    }
    return NULL;
}

/* Capture a few pixels in the middle of each zoom-block area the way a
 * screen-sharing program would. If Windows is protecting the area they are
 * black; if they are not, the content is leaking. */
static int check_capture(void)
{
    enum { N = 8 };
    int zoom = 0, passed = 0, failed = 0;
    HDC screen = GetDC(NULL);
    HDC mem = CreateCompatibleDC(screen);
    BITMAPINFO bi;
    ZeroMemory(&bi, sizeof bi);
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = N;
    bi.bmiHeader.biHeight = -N;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    DWORD *bits = NULL;
    HBITMAP bmp = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, (void **)&bits, NULL, 0);
    HBITMAP old = bmp ? SelectObject(mem, bmp) : NULL;
    for (int i = 0; i < g_count; i++) {
        Area *a = &g_areas[i];
        if (a->mode != ZOOM) continue;
        zoom++;
        if (!a->affinity_ok) { failed++; continue; }   /* Windows refused the protection */
        if (!bmp || !bits) continue;
        POINT c = { (a->rc.left + a->rc.right) / 2, (a->rc.top + a->rc.bottom) / 2 };
        if (!MonitorFromPoint(c, MONITOR_DEFAULTTONULL)) continue;   /* off screen */
        /* Only judge the area when its own window is the top one at that
         * spot; otherwise the pixels belong to something else. */
        if (top_window_at(c) != a->block) continue;
        for (int k = 0; k < N * N; k++) bits[k] = 0x00FFFFFF;
        if (!BitBlt(mem, 0, 0, N, N, screen, c.x - N / 2, c.y - N / 2, SRCCOPY | CAPTUREBLT))
            continue;
        GdiFlush();
        BOOL black = TRUE;
        for (int k = 0; k < N * N; k++)
            if (bits[k] & 0x00FFFFFF) { black = FALSE; break; }
        if (black) passed++; else failed++;
    }
    if (old) SelectObject(mem, old);
    if (bmp) DeleteObject(bmp);
    DeleteDC(mem);
    ReleaseDC(NULL, screen);
    if (zoom == 0) return CHECK_NONE;
    if (failed) return CHECK_FAIL;
    return passed == zoom ? CHECK_OK : CHECK_UNKNOWN;
}

static void run_check(void)
{
    if (g_drag.hwnd || g_draw) return;
    int now = check_capture();
    if (now == CHECK_FAIL) {
        /* A window that was changed a moment ago may not be on screen yet:
         * only believe a failure that is still there on the next look. */
        if (++g_fail_streak < 3) {
            schedule_check();
            return;
        }
    } else {
        g_fail_streak = 0;
    }
    if (now != g_check) {
        g_check = now;
        SetWindowTextW(g_status, now == CHECK_OK ? TXT_OK : now == CHECK_FAIL ? TXT_FAIL
                                 : now == CHECK_UNKNOWN ? TXT_UNKNOWN : TXT_NONE);
        InvalidateRect(g_status, NULL, TRUE);
    }
    if (now == CHECK_FAIL && !g_warned) {
        g_warned = TRUE;
        MessageBoxW(g_panel,
            L"Zoom block is not working on this PC.\n\n"
            L"Screen Mask captured the screen the way a screen-sharing app does, and the "
            L"content under a zoom-block area was still visible. People you share your "
            L"screen with would see it too.\n\n"
            L"Pitch-black areas are not affected: they are black for everyone.",
            APP_NAME, MB_OK | MB_ICONWARNING | MB_SETFOREGROUND);
    }
}

static void schedule_check(void)
{
    if (g_panel) SetTimer(g_panel, TIMER_CHECK_SOON, 500, NULL);
}

/* ------------------------------------------------------------- the panel */

static void refresh_list(void)
{
    if (!g_list) return;
    int selected = (int)SendMessageW(g_list, LB_GETCURSEL, 0, 0);
    SendMessageW(g_list, WM_SETREDRAW, FALSE, 0);
    SendMessageW(g_list, LB_RESETCONTENT, 0, 0);
    for (int i = 0; i < g_count; i++) {
        wchar_t text[128];
        const RECT *r = &g_areas[i].rc;
        swprintf(text, 128, L"%ls     %ld \x00D7 %ld at %ld, %ld",
                 g_areas[i].mode == PITCH ? L"Pitch black" : L"Zoom block ",
                 r->right - r->left, r->bottom - r->top, r->left, r->top);
        SendMessageW(g_list, LB_ADDSTRING, 0, (LPARAM)text);
    }
    if (selected >= 0 && selected < g_count) SendMessageW(g_list, LB_SETCURSEL, selected, 0);
    SendMessageW(g_list, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(g_list, NULL, TRUE);
    BOOL has = SendMessageW(g_list, LB_GETCURSEL, 0, 0) != LB_ERR;
    EnableWindow(GetDlgItem(g_panel, ID_SWITCH), has);
    EnableWindow(GetDlgItem(g_panel, ID_REMOVE), has);
}

static HWND control(HWND parent, LPCWSTR cls, LPCWSTR text, DWORD style, int x, int y,
                    int w, int h, int id, HFONT font)
{
    HWND c = CreateWindowExW(wcscmp(cls, L"LISTBOX") == 0 ? WS_EX_CLIENTEDGE : 0, cls, text,
                             WS_CHILD | WS_VISIBLE | style, x, y, w, h, parent,
                             (HMENU)(INT_PTR)id, g_inst, NULL);
    SendMessageW(c, WM_SETFONT, (WPARAM)font, TRUE);
    return c;
}

/* A wrapped label, as tall as its text needs with the font in use. */
static int label(HWND parent, LPCWSTR text, int x, int y, int w, int id, int min_lines)
{
    HDC dc = GetDC(parent);
    HFONT old = SelectObject(dc, g_font);
    RECT r = { 0, 0, w, 0 };
    DrawTextW(dc, text, -1, &r, DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
    TEXTMETRICW tm;
    GetTextMetricsW(dc, &tm);
    SelectObject(dc, old);
    ReleaseDC(parent, dc);
    int h = r.bottom + S(2);
    if (h < min_lines * tm.tmHeight + S(2)) h = min_lines * tm.tmHeight + S(2);
    HWND c = control(parent, L"STATIC", text, SS_NOPREFIX, x, y, w, h, id, g_font);
    if (id == ID_STATUS) g_status = c;
    return h;
}

static int build_panel(HWND hwnd)
{
    int m = S(16), w = S(420), y = S(14), gap = S(8), half = (w - gap) / 2;

    control(hwnd, L"STATIC", L"Areas", 0, m, y, w, S(18), 0, g_bold);
    y += S(24);
    control(hwnd, L"BUTTON", L"Add pitch-black area", WS_TABSTOP, m, y, half, S(30),
            ID_ADD_PITCH, g_font);
    control(hwnd, L"BUTTON", L"Add zoom-block area", WS_TABSTOP, m + half + gap, y, half, S(30),
            ID_ADD_ZOOM, g_font);
    y += S(38);
    y += label(hwnd,
               L"Pitch black hides the area from everyone, including you. Zoom block keeps "
               L"it visible to you and shows black to anyone your screen is shared with.",
               m, y, w, 0, 1) + S(8);
    g_list = control(hwnd, L"LISTBOX", L"",
                     WS_TABSTOP | WS_VSCROLL | LBS_NOTIFY | LBS_NOINTEGRALHEIGHT,
                     m, y, w, S(112), ID_LIST, g_font);
    y += S(120);
    control(hwnd, L"BUTTON", L"Switch type", WS_TABSTOP, m, y, half, S(28), ID_SWITCH, g_font);
    control(hwnd, L"BUTTON", L"Remove", WS_TABSTOP, m + half + gap, y, half, S(28), ID_REMOVE,
            g_font);
    y += S(40);
    g_edit_box = control(hwnd, L"BUTTON",
                         L"Edit areas (drag to move, drag an edge to resize)",
                         WS_TABSTOP | BS_AUTOCHECKBOX, m, y, w, S(22), ID_EDIT, g_font);
    y += S(26);
    g_outline_box = control(hwnd, L"BUTTON", L"Show an outline around zoom-block areas",
                            WS_TABSTOP | BS_AUTOCHECKBOX, m, y, w, S(22), ID_OUTLINE, g_font);
    Button_SetCheck(g_outline_box, g_outlines ? BST_CHECKED : BST_UNCHECKED);
    y += S(36);
    control(hwnd, L"STATIC", L"Screen sharing", 0, m, y, w, S(18), 0, g_bold);
    y += S(24);
    y += label(hwnd,
               L"In Zoom, Meet or Teams share your entire screen. Zoom block does not "
               L"apply when you share a single app window, because the black rectangle is "
               L"not part of that window.",
               m, y, w, 0, 1) + S(10);
    /* sized for the longest status text */
    y += label(hwnd, TXT_UNKNOWN, m, y, w, ID_STATUS, 2) + S(14);
    SetWindowTextW(g_status, L"");
    return y;
}

static LRESULT CALLBACK PanelProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_COMMAND:
        switch (LOWORD(wp)) {
        case ID_ADD_PITCH: begin_drawing(PITCH); break;
        case ID_ADD_ZOOM:  begin_drawing(ZOOM); break;
        case ID_SWITCH:    switch_area((int)SendMessageW(g_list, LB_GETCURSEL, 0, 0)); break;
        case ID_REMOVE:    remove_area((int)SendMessageW(g_list, LB_GETCURSEL, 0, 0)); break;
        case ID_EDIT:      set_editing(Button_GetCheck(g_edit_box) == BST_CHECKED); break;
        case ID_OUTLINE:
            g_outlines = Button_GetCheck(g_outline_box) == BST_CHECKED;
            apply_all();
            save_config();
            break;
        case ID_LIST:
            if (HIWORD(wp) == LBN_SELCHANGE) {
                EnableWindow(GetDlgItem(hwnd, ID_SWITCH), TRUE);
                EnableWindow(GetDlgItem(hwnd, ID_REMOVE), TRUE);
            }
            break;
        }
        return 0;
    case WM_TIMER:
        if (wp == TIMER_TOPMOST) {
            /* stay above other always-on-top windows that appeared later */
            if (!g_in_menu && !g_draw && !g_drag.hwnd)
                for (int i = 0; i < g_count; i++) {
                    SetWindowPos(g_areas[i].block, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                    if (g_areas[i].outline)
                        SetWindowPos(g_areas[i].outline, HWND_TOPMOST, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                }
        } else if (wp == TIMER_CHECK_SOON) {
            KillTimer(hwnd, TIMER_CHECK_SOON);
            run_check();
        } else if (wp == TIMER_CHECK) {
            run_check();
        }
        return 0;
    case WM_CTLCOLORSTATIC:
        if ((HWND)lp == g_status) {
            HDC dc = (HDC)wp;
            SetBkMode(dc, TRANSPARENT);
            SetTextColor(dc, g_check == CHECK_FAIL ? RGB(192, 0, 0)
                           : g_check == CHECK_OK ? RGB(0, 110, 40)
                           : g_check == CHECK_UNKNOWN ? RGB(150, 90, 0)
                           : GetSysColor(COLOR_GRAYTEXT));
            return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
        }
        break;
    case WM_CLOSE:
        if (g_count > 0 &&
            MessageBoxW(hwnd,
                        L"Quit Screen Mask?\n\nAll black areas disappear. To keep them and "
                        L"just get this window out of the way, minimise it instead.",
                        APP_NAME, MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) != IDYES)
            return 0;
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        save_config();
        end_drawing();
        for (int i = 0; i < g_count; i++) {
            if (g_areas[i].outline) DestroyWindow(g_areas[i].outline);
            DestroyWindow(g_areas[i].block);
        }
        g_count = 0;
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static void register_class(LPCWSTR name, WNDPROC proc, HBRUSH background, HICON icon)
{
    WNDCLASSEXW wc;
    ZeroMemory(&wc, sizeof wc);
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = proc;
    wc.hInstance = g_inst;
    wc.hCursor = LoadCursorW(NULL, IDC_ARROW);
    wc.hbrBackground = background;
    wc.lpszClassName = name;
    wc.hIcon = icon;
    wc.hIconSm = icon;
    RegisterClassExW(&wc);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmdline, int show)
{
    (void)prev; (void)cmdline;
    g_inst = inst;

    /* one copy at a time: a second start just brings the first one forward */
    CreateMutexW(NULL, TRUE, L"ScreenMaskSingleInstance");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND other = FindWindowW(CLS_PANEL, NULL);
        if (other) {
            ShowWindow(other, IsIconic(other) ? SW_RESTORE : SW_SHOW);
            SetForegroundWindow(other);
        }
        return 0;
    }

    INITCOMMONCONTROLSEX icc = { sizeof icc, ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);

    HDC screen = GetDC(NULL);
    g_dpi = GetDeviceCaps(screen, LOGPIXELSX);
    ReleaseDC(NULL, screen);
    if (g_dpi < 96) g_dpi = 96;
    g_font = CreateFontW(-MulDiv(9, g_dpi, 72), 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                         0, 0, CLEARTYPE_QUALITY, 0, L"Segoe UI");
    g_bold = CreateFontW(-MulDiv(9, g_dpi, 72), 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
                         0, 0, CLEARTYPE_QUALITY, 0, L"Segoe UI");

    HICON icon = LoadIconW(inst, MAKEINTRESOURCEW(1));
    register_class(CLS_PANEL, PanelProc, GetSysColorBrush(COLOR_BTNFACE), icon);
    register_class(CLS_AREA, AreaProc, NULL, NULL);
    register_class(CLS_OUTLINE, OutlineProc, NULL, NULL);
    register_class(CLS_DRAW, DrawProc, NULL, NULL);

    DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    g_panel = CreateWindowExW(0, CLS_PANEL, APP_NAME, style, CW_USEDEFAULT, CW_USEDEFAULT,
                              S(460), S(400), NULL, NULL, inst, NULL);
    if (!g_panel) return 1;
    int height = build_panel(g_panel);
    RECT wr = { 0, 0, S(420) + 2 * S(16), height };
    AdjustWindowRectEx(&wr, style, FALSE, 0);
    SetWindowPos(g_panel, NULL, 0, 0, wr.right - wr.left, wr.bottom - wr.top,
                 SWP_NOMOVE | SWP_NOZORDER);

    load_config();
    refresh_list();
    run_check();                          /* sets the first status text */
    SetTimer(g_panel, TIMER_TOPMOST, 2000, NULL);
    SetTimer(g_panel, TIMER_CHECK, 5000, NULL);
    schedule_check();

    ShowWindow(g_panel, show);
    UpdateWindow(g_panel);

    MSG msg;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        if (IsDialogMessageW(g_panel, &msg)) continue;
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return 0;
}
