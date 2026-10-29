"""Classic Windows Shell menu in a separate process; no registry changes."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import ntpath
import sys
import uuid

P = C.c_void_p
HRESULT = C.c_long
LRESULT = C.c_ssize_t
UINT_PTR = C.c_size_t


class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


class InvokeInfo(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('fMask', W.DWORD), ('hwnd', W.HWND),
                ('lpVerb', P), ('lpParameters', C.c_char_p), ('lpDirectory', C.c_char_p),
                ('nShow', C.c_int), ('dwHotKey', W.DWORD), ('hIcon', W.HANDLE),
                ('lpTitle', C.c_char_p), ('lpVerbW', P), ('lpParametersW', W.LPCWSTR),
                ('lpDirectoryW', W.LPCWSTR), ('lpTitleW', W.LPCWSTR), ('ptInvoke', W.POINT)]


def checked(code):
    if code < 0:
        raise OSError(f'Windows Shell error 0x{code & 0xffffffff:08X}')
    return code


def method(obj, slot, result, *args):
    table = C.cast(obj, C.POINTER(C.POINTER(P))).contents
    return C.WINFUNCTYPE(result, P, *args)(table[slot])


def api(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = list(args)
    return function


CUSTOM = {0x7000: 'ask', 0x7001: 'reveal', 0x7002: 'copy_path',
          0x7003: 'copy_to', 0x7004: 'move_to', 0x7005: 'paste'}
ROUTED = {'delete': 'recycle', 'rename': 'rename', 'copy': 'copy', 'cut': 'cut', 'paste': 'paste', 'properties': 'properties'}


def show_menu(request, *, probe=False):
    paths = request['paths']
    if not paths or any(not ntpath.isabs(p) or '\0' in p for p in paths):
        raise ValueError('Choose existing absolute file paths.')
    parents = {ntpath.normcase(ntpath.dirname(p)) for p in paths}
    if len(parents) != 1:
        # ponytail: Shell GetUIObjectOf requires siblings. The SAGE menu supports
        # mixed-parent selections; do not silently operate on only the first item.
        return {'fallback': True}
    shell = C.WinDLL('shell32', use_last_error=True)
    user = C.WinDLL('user32', use_last_error=True)
    ole = C.WinDLL('ole32', use_last_error=True)
    comctl = C.WinDLL('comctl32', use_last_error=True)
    init = api(ole, 'OleInitialize', HRESULT, P)
    checked(init(None))
    free = api(ole, 'CoTaskMemFree', None, P)
    release = lambda obj: method(obj, 2, W.ULONG)(obj) if obj else None
    pidls = []
    parent, menu_obj, extended = P(), P(), P()
    popup, hwnd, subclass = None, None, None
    try:
        parse = api(shell, 'SHParseDisplayName', HRESULT, W.LPCWSTR, P, C.POINTER(P), W.DWORD, P)
        for path in paths:
            pidl = P()
            checked(parse(path, None, C.byref(pidl), 0, None))
            pidls.append(pidl)
        child = P()
        iid_folder = GUID('000214e6-0000-0000-c000-000000000046')
        checked(api(shell, 'SHBindToParent', HRESULT, P, C.POINTER(GUID), C.POINTER(P), C.POINTER(P))(
            pidls[0], C.byref(iid_folder), C.byref(parent), C.byref(child)))
        last_id = api(shell, 'ILFindLastID', P, P)
        children = (P * len(pidls))(*(last_id(pidl) for pidl in pidls))
        hwnd = api(user, 'CreateWindowExW', W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                   C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, P)(
                       0, 'STATIC', 'SAGE menu', 0x80000000, request['x'], request['y'], 0, 0,
                       None, None, None, None)
        if not hwnd:
            raise C.WinError(C.get_last_error())
        iid_menu = GUID('000214e4-0000-0000-c000-000000000046')
        checked(method(parent, 10, HRESULT, W.HWND, W.UINT, C.POINTER(P), C.POINTER(GUID), P, C.POINTER(P))(
            parent, hwnd, len(paths), children, C.byref(iid_menu), None, C.byref(menu_obj)))
        popup = api(user, 'CreatePopupMenu', W.HMENU)()
        if not popup:
            raise C.WinError(C.get_last_error())
        checked(method(menu_obj, 3, HRESULT, W.HMENU, W.UINT, W.UINT, W.UINT, W.UINT)(
            menu_obj, popup, 0, 1, 0x6fff, 0x10 | (0x100 if request.get('shift') else 0)))
        # Extensions need forwarded messages for dynamic and owner-drawn submenus.
        version = 0
        for guid, candidate in [('bcfce0a0-ec17-11d0-8d10-00a0c90f2719', 3),
                                ('000214f4-0000-0000-c000-000000000046', 2)]:
            iid = GUID(guid)
            if method(menu_obj, 0, HRESULT, C.POINTER(GUID), C.POINTER(P))(
                    menu_obj, C.byref(iid), C.byref(extended)) >= 0:
                version = candidate
                break
        callback_type = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM, UINT_PTR, UINT_PTR)
        default_proc = api(comctl, 'DefSubclassProc', LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        @callback_type
        def subclass(hwnd, msg, wp, lp, _id, _data):
            if extended and msg in {0x2b, 0x2c, 0x117, 0x120}:
                if version == 3:
                    result = LRESULT()
                    code = method(extended, 7, HRESULT, W.UINT, W.WPARAM, W.LPARAM, C.POINTER(LRESULT))(
                        extended, msg, wp, lp, C.byref(result))
                    if code == 0:
                        return result.value
                elif msg != 0x120:
                    code = method(extended, 6, HRESULT, W.UINT, W.WPARAM, W.LPARAM)(extended, msg, wp, lp)
                    if code == 0:
                        return 0
            return default_proc(hwnd, msg, wp, lp)
        if not api(comctl, 'SetWindowSubclass', W.BOOL, W.HWND, callback_type, UINT_PTR, UINT_PTR)(hwnd, subclass, 1, 0):
            raise C.WinError(C.get_last_error())
        insert = api(user, 'InsertMenuW', W.BOOL, W.HMENU, W.UINT, W.UINT, UINT_PTR, W.LPCWSTR)
        entries = [(0x7000, 'Ask SAGE', len(paths) == 1), (0x7001, 'Show in Explorer', True),
                   (0x7002, 'Copy path', True), (0x7003, 'Copy to…', True), (0x7004, 'Move to…', True),
                   (0x7005, 'Paste (SAGE clipboard)', bool(request.get('paste')))]
        for index, (command, label, enabled) in enumerate(entries):
            insert(popup, index, 0x400 | (0 if enabled else 3), command, label)
        insert(popup, len(entries), 0x400 | 0x800, 0, None)
        if probe:
            count = api(user, 'GetMenuItemCount', C.c_int, W.HMENU)(popup)
            labels = []
            verbs = []
            read = api(user, 'GetMenuStringW', C.c_int, W.HMENU, W.UINT, W.LPWSTR, C.c_int, W.UINT)
            get_id = api(user, 'GetMenuItemID', W.UINT, W.HMENU, C.c_int)
            for index in range(count):
                label = C.create_unicode_buffer(512)
                read(popup, index, label, len(label), 0x400)
                labels.append(label.value)
                command = get_id(popup, index)
                verb = C.create_unicode_buffer(256)
                if 1 <= command <= 0x6fff:
                    method(menu_obj, 5, HRESULT, UINT_PTR, W.UINT, P, W.LPWSTR, W.UINT)(
                        menu_obj, command-1, 4, None, verb, len(verb))
                verbs.append(verb.value)
            return {'labels': labels, 'verbs': verbs, 'extended_interface': version}
        # A visible, zero-size owner lets outside clicks dismiss the popup normally.
        api(user, 'ShowWindow', W.BOOL, W.HWND, C.c_int)(hwnd, 4)
        api(user, 'SetForegroundWindow', W.BOOL, W.HWND)(hwnd)
        command = api(user, 'TrackPopupMenuEx', W.UINT, W.HMENU, W.UINT, C.c_int, C.c_int, W.HWND, P)(
            popup, 0x100 | 0x2, request['x'], request['y'], hwnd, None)
        if not command:
            return {'cancelled': True}
        if command in CUSTOM:
            return {'action': CUSTOM[command]}
        verb = C.create_unicode_buffer(256)
        method(menu_obj, 5, HRESULT, UINT_PTR, W.UINT, P, W.LPWSTR, W.UINT)(
            menu_obj, command-1, 4, None, verb, len(verb))
        canonical = verb.value.casefold()
        if canonical in ROUTED:
            return {'action': ROUTED[canonical]}
        info = InvokeInfo()
        info.cbSize = C.sizeof(info)
        info.fMask = 0x4000 | 0x100 | 0x20000000  # Unicode, no async, invocation point
        info.hwnd, info.nShow = hwnd, 1
        info.lpVerb = info.lpVerbW = command-1
        info.ptInvoke = W.POINT(request['x'], request['y'])
        code = method(menu_obj, 4, HRESULT, C.POINTER(InvokeInfo))(menu_obj, C.byref(info))
        return {'invoked': True, 'verb': canonical, 'error': f'Windows command failed (0x{code & 0xffffffff:08X}).' if code < 0 else None}
    finally:
        if hwnd:
            if subclass:
                api(comctl, 'RemoveWindowSubclass', W.BOOL, W.HWND, callback_type, UINT_PTR)(hwnd, subclass, 1)
            api(user, 'DestroyWindow', W.BOOL, W.HWND)(hwnd)
        if popup:
            api(user, 'DestroyMenu', W.BOOL, W.HMENU)(popup)
        release(extended)
        release(menu_obj)
        release(parent)
        for pidl in pidls:
            free(pidl)
        api(ole, 'OleUninitialize', None)()


if __name__ == '__main__':
    try:
        response = show_menu(json.loads(sys.stdin.read()), probe='--probe' in sys.argv)
    except Exception as exc:
        response = {'fallback': True, 'error': str(exc)}
    print(json.dumps(response, ensure_ascii=True))
