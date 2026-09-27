"""CI-only sandbox feasibility probe; never traverse or serialize job data."""
import ctypes
import os
import sys


def main():
    if sys.platform != "darwin":
        return 1
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sm = ctypes.CDLL("/System/Library/Frameworks/ServiceManagement.framework/ServiceManagement")
        signatures = {
            "CFStringCreateWithCString": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]),
            "CFGetTypeID": (ctypes.c_ulong, [ctypes.c_void_p]),
            "CFDictionaryGetTypeID": (ctypes.c_ulong, []),
            "CFArrayGetTypeID": (ctypes.c_ulong, []),
            "CFStringGetTypeID": (ctypes.c_ulong, []),
            "CFNumberGetTypeID": (ctypes.c_ulong, []),
            "CFDictionaryGetValue": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
            "CFArrayGetCount": (ctypes.c_long, [ctypes.c_void_p]),
            "CFArrayGetValueAtIndex": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_long]),
            "CFStringGetCString": (ctypes.c_bool, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32]),
            "CFNumberGetValue": (ctypes.c_bool, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]),
            "CFRelease": (None, [ctypes.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(cf, name)
            function.restype, function.argtypes = result, args
        sm.SMJobCopyDictionary.restype = ctypes.c_void_p
        sm.SMJobCopyDictionary.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        label = cf.CFStringCreateWithCString(None, b"com.oshioki.agent", 0x08000100)
        domain = ctypes.c_void_p.in_dll(sm, "kSMDomainUserLaunchd")
        response = None
        try:
            response = sm.SMJobCopyDictionary(domain, label)
            if not response:
                print("SM sandbox probe: dictionary unavailable", file=sys.stderr)
                return 0
            if cf.CFGetTypeID(response) != cf.CFDictionaryGetTypeID():
                print("SM sandbox probe: unexpected dictionary type", file=sys.stderr)
                return 0

            def field(name):
                key = cf.CFStringCreateWithCString(None, name, 0x08000100)
                try:
                    return cf.CFDictionaryGetValue(response, key)
                finally:
                    cf.CFRelease(key)

            program = field(b"Program")
            if not program:
                arguments = field(b"ProgramArguments")
                if arguments and cf.CFGetTypeID(arguments) == cf.CFArrayGetTypeID() and cf.CFArrayGetCount(arguments):
                    program = cf.CFArrayGetValueAtIndex(arguments, 0)
            pid = field(b"PID")
            buffer = ctypes.create_string_buffer(4096)
            number = ctypes.c_longlong(0)
            valid_program = bool(program and cf.CFGetTypeID(program) == cf.CFStringGetTypeID()
                                 and cf.CFStringGetCString(program, buffer, len(buffer), 0x08000100))
            valid_pid = bool(pid and cf.CFGetTypeID(pid) == cf.CFNumberGetTypeID()
                             and cf.CFNumberGetValue(pid, 4, ctypes.byref(number)))
            expected = os.fsencode(sys.argv[1] + "/Oshioki.app/Contents/MacOS/oshioki-agent")
            # Output only fixed field names and boolean results. No dictionary,
            # program string, process arguments or environment leaves the probe.
            print(f"SM sandbox probe: dictionary available; program_matches={valid_program and buffer.value == expected}; "
                  f"pid_positive={valid_pid and number.value > 0}", file=sys.stderr)
            return 0
        finally:
            if response:
                cf.CFRelease(response)
            cf.CFRelease(label)
    except (OSError, AttributeError, ValueError):
        print("SM sandbox probe: framework unavailable", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
