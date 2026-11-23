// camera-probe: prints "on" or "off" whenever any camera starts or stops being
// used by any process. The robust alternative to parsing the unified log.
//
// It polls CoreMediaIO's kCMIODevicePropertyDeviceIsRunningSomewhere once a
// second. Reading that property does not open the camera, so it needs no
// camera permission and never lights the indicator itself.
//
// Build: swiftc -O camera-probe.swift -o camera-probe
// (install-camera-trigger.sh --swift does this and wires it into the watcher.)

import CoreMediaIO
import Foundation

func address(_ selector: Int) -> CMIOObjectPropertyAddress {
    CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(selector),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
}

func cameras() -> [CMIOObjectID] {
    var devices = address(kCMIOHardwarePropertyDevices)
    let system = CMIOObjectID(kCMIOObjectSystemObject)
    var size: UInt32 = 0
    guard CMIOObjectGetPropertyDataSize(system, &devices, 0, nil, &size) == 0, size > 0 else {
        return []
    }
    var ids = [CMIOObjectID](repeating: 0, count: Int(size) / MemoryLayout<CMIOObjectID>.size)
    var used: UInt32 = 0
    guard CMIOObjectGetPropertyData(system, &devices, 0, nil, size, &used, &ids) == 0 else {
        return []
    }
    return Array(ids.prefix(Int(used) / MemoryLayout<CMIOObjectID>.size))
}

func isRunning(_ device: CMIOObjectID) -> Bool {
    var running = address(kCMIODevicePropertyDeviceIsRunningSomewhere)
    var value: UInt32 = 0
    var used: UInt32 = 0
    let size = UInt32(MemoryLayout<UInt32>.size)
    return CMIOObjectGetPropertyData(device, &running, 0, nil, size, &used, &value) == 0 && value != 0
}

setvbuf(stdout, nil, _IOLBF, 0)  // the watcher reads this through a pipe
var last: Bool? = nil
func poll() {
    let inUse = cameras().contains(where: isRunning)
    if inUse != last {
        print(inUse ? "on" : "off")
        last = inUse
    }
}

// Poll from a timer on the main run loop rather than a sleep loop: CoreMediaIO
// only notices a camera being plugged in or unplugged while the run loop is
// serviced, so a sleep loop keeps watching the cameras present at launch.
poll()
Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in poll() }
RunLoop.main.run()
