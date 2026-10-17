#include "vacuula_installer.h"
#include "neato_serial.h"
#include "data_logger.h"

// Robot software builds whose cloud client Vacuula's scripts know how to patch
// (their vacuula.sh carries byte offsets per build). GetVersion reports these
// as "4,5,3,189,0" / "4,6,0,72,0"; both sides are reduced to four components.
static const char *const SUPPORTED_FIRMWARE[] = {"4.5.3.189", "4.6.0.72"};

// Scoped mutex hold.
struct Locked {
    SemaphoreHandle_t m;
    explicit Locked(SemaphoreHandle_t mutex) : m(mutex) { xSemaphoreTake(m, portMAX_DELAY); }
    ~Locked() { xSemaphoreGive(m); }
};

// "4.5.3.189.0" / "4.5.3-189" / "4,5,3,189" -> "4.5.3.189"
static String normalizeVersion(const String& version) {
    String out;
    String part;
    int parts = 0;
    for (unsigned int i = 0; i < version.length(); i++) {
        char c = version.charAt(i);
        if (isDigit(c)) {
            part += c;
            continue;
        }
        if (c != '.' && c != '-' && c != ',')
            break;
        if (part.length() > 0) {
            if (parts > 0)
                out += '.';
            out += part;
            part = "";
            if (++parts == 4)
                return out;
        }
    }
    if (part.length() > 0 && parts < 4) {
        if (parts > 0)
            out += '.';
        out += part;
    }
    return out;
}

std::vector<Field> VacuulaStatus::toFields() const {
    return {
            {"firmware", firmware, FIELD_STRING},
            {"model", model, FIELD_STRING},
            {"serial", serial, FIELD_STRING},
            {"firmwareSupported", firmwareSupported ? "true" : "false", FIELD_BOOL},
            {"skeyReady", skeyReady ? "true" : "false", FIELD_BOOL},
            {"phase", phase, FIELD_STRING},
            {"bytesSent", String(bytesSent), FIELD_INT},
            {"bytesTotal", String(bytesTotal), FIELD_INT},
            {"message", message, FIELD_STRING},
            {"resetRequested", resetRequested ? "true" : "false", FIELD_BOOL},
            {"resetSent", resetSent ? "true" : "false", FIELD_BOOL},
            {"resetError", resetError, FIELD_STRING},
            {"quiet", quiet ? "true" : "false", FIELD_BOOL},
            {"quietRemainingMs", String(quietRemainingMs), FIELD_INT},
            {"logNext", String(logNext), FIELD_INT},
    };
}

VacuulaInstaller::VacuulaInstaller(NeatoSerial& neato, DataLogger& logger) :
    neato(neato), dataLogger(logger), lock(xSemaphoreCreateMutex()) {}

void VacuulaInstaller::begin() {
    neato.setIdleLineCallback([this](const String& line) { onConsoleLine(line); });
}

bool VacuulaInstaller::isSupportedFirmware(const String& softwareVersion) {
    String normalized = normalizeVersion(softwareVersion);
    if (normalized.isEmpty())
        return false;
    for (const char *supported: SUPPORTED_FIRMWARE) {
        if (normalized == supported)
            return true;
    }
    return false;
}

// -- Archive reception -------------------------------------------------------

// Why a `total` byte archive cannot be accepted right now, or "" if it can.
static String receiveRefusal(bool uploading, bool starting, size_t total) {
    if (uploading)
        return "an upload is already in progress";
    if (starting)
        return "an upload is being started";
    if (total == 0)
        return "empty body (chunked transfer encoding is not supported, send Content-Length)";
    if (total > VACUULA_UPLOAD_MAX_BYTES)
        return "archive larger than " + String(VACUULA_UPLOAD_MAX_BYTES) + " bytes";
    if (ESP.getMaxAllocHeap() < total + 8192)
        return "not enough free memory for a " + String(total) + " byte archive";
    return "";
}

bool VacuulaInstaller::beginReceive(const void *owner, size_t total) {
    Locked guard(lock);
    String refusal = receiveRefusal(neato.isUploading(), startPending, total);
    if (!refusal.isEmpty()) {
        // Leave an archive another request is still working on untouched.
        if (!startPending)
            receiveError = refusal;
        LOG("VACUULA", "Receive refused: %s", refusal.c_str());
        return false;
    }

    std::vector<uint8_t>().swap(archive);
    archive.reserve(total);
    receiveOwner = owner;
    expectedBytes = total;
    receiveDone = false;
    receiveError = "";
    return true;
}

void VacuulaInstaller::receiveChunk(const void *owner, const uint8_t *data, size_t len) {
    Locked guard(lock);
    if (owner != receiveOwner || !receiveError.isEmpty() || expectedBytes == 0 || len == 0)
        return;
    if (archive.size() + len > expectedBytes) {
        receiveError = "body longer than announced";
        std::vector<uint8_t>().swap(archive);
        expectedBytes = 0;
        return;
    }
    archive.insert(archive.end(), data, data + len);
    if (archive.size() == expectedBytes) {
        receiveDone = true;
        LOG("VACUULA", "Archive received (%u bytes)", archive.size());
    }
}

bool VacuulaInstaller::receiveComplete(const void *owner) {
    Locked guard(lock);
    return receiveDone && owner == receiveOwner && !archive.empty();
}

String VacuulaInstaller::getReceiveError() {
    Locked guard(lock);
    return receiveError;
}

// -- Actions -----------------------------------------------------------------

void VacuulaInstaller::start(bool reset, bool force, std::function<void(bool, const String&)> callback) {
    {
        Locked guard(lock);
        if (!receiveDone || archive.empty()) {
            callback(false, receiveError.isEmpty() ? "no archive received" : receiveError);
            return;
        }
        if (startPending) {
            callback(false, "an upload is being started");
            return;
        }
        startPending = true;
    }
    if (neato.isUploading() || neato.isQueuePaused() || neato.isManualCleanActive()) {
        Locked guard(lock);
        startPending = false;
        callback(false, neato.isUploading()           ? "an upload is already in progress"
                        : neato.isManualCleanActive() ? "manual clean is active; stop it first"
                                                      : "robot polling is paused (quiet window); POST "
                                                        "/api/vacuula/resume first");
        return;
    }

    // The callback may run inline (version cache hit) or later on the loop
    // task (cache miss); either way the mutex is not held across the call.
    neato.getVersion([this, reset, force, callback](bool ok, const VersionData& v) {
        std::vector<uint8_t> data;
        {
            Locked guard(lock);
            startPending = false;
            if (!ok) {
                callback(false, "robot did not answer GetVersion");
                return;
            }
            lastVersion = v;
            haveVersion = true;

            if (!force && !isSupportedModel(v.modelName)) {
                callback(false, "robot model " + v.modelName + " is not a Botvac D3-D7; add force=1 to override");
                return;
            }
            if (!force && !isSupportedFirmware(v.softwareVersion)) {
                callback(false, "robot firmware " + v.softwareVersion +
                                        " is not one Vacuula's scripts know (4.5.3-189, 4.6.0-72); add force=1 to "
                                        "override");
                return;
            }
            if (reset && !neato.hasSKey()) {
                callback(false, "SKey not ready yet, cannot reset the network");
                return;
            }
            data.swap(archive);
            receiveDone = false;
            receiveOwner = nullptr;
            expectedBytes = 0;
        }

        resetRequested = reset;
        resetSent = false;
        resetError = "";
        size_t bytes = data.size();
        bool started = neato.uploadFile("Tarball", std::move(data),
                                        [this](bool okUpload, const String& msg) { onUploadDone(okUpload, msg); });
        if (!started) {
            callback(false, "could not start the upload");
            return;
        }

        dataLogger.logGenericEvent("vacuula", {{"event", "upload_start", FIELD_STRING},
                                               {"bytes", String(bytes), FIELD_INT},
                                               {"reset", reset ? "true" : "false", FIELD_BOOL}});
        callback(true, "");
    });
}

void VacuulaInstaller::onUploadDone(bool ok, const String& message) {
    dataLogger.logGenericEvent("vacuula", {{"event", ok ? "upload_ok" : "upload_failed", FIELD_STRING},
                                           {"message", message, FIELD_STRING}});
    if (ok && resetRequested && !sendReset())
        resetError = "network reset could not be queued";
}

bool VacuulaInstaller::sendReset() {
    return neato.resetNetwork([this](bool ok) {
        resetSent = ok;
        if (!ok) {
            resetError = "network reset command was not sent";
            LOG("VACUULA", "%s", resetError.c_str());
            return;
        }
        // The robot now drops its WiFi and starts its setup access point. Stay
        // off the console so its install script runs undisturbed, and keep what
        // it prints there for the PC-side script.
        {
            Locked guard(lock);
            logLines.clear();
        }
        neato.pauseQueue(VACUULA_QUIET_MS);
        dataLogger.logGenericEvent("vacuula", {{"event", "network_reset", FIELD_STRING}});
    });
}

bool VacuulaInstaller::resetNetwork(String& error) {
    if (neato.isQueuePaused()) {
        error = "robot polling is paused (quiet window); POST /api/vacuula/resume first";
        return false;
    }
    if (neato.isManualCleanActive()) {
        error = "manual clean is active; stop it first";
        return false;
    }
    if (!neato.hasSKey()) {
        error = "SKey not ready yet";
        return false;
    }
    resetRequested = true;
    resetSent = false;
    resetError = "";
    if (!sendReset()) {
        error = "command queue full";
        return false;
    }
    return true;
}

void VacuulaInstaller::armQuiet(unsigned long ms) {
    if (ms == 0 || ms > VACUULA_QUIET_MAX_MS)
        ms = VACUULA_QUIET_MS;
    neato.pauseQueue(ms);
    dataLogger.logGenericEvent("vacuula", {{"event", "quiet", FIELD_STRING}, {"ms", String(ms), FIELD_INT}});
}

void VacuulaInstaller::endQuiet() {
    neato.resumeQueue();
}

// -- Reporting ---------------------------------------------------------------

void VacuulaInstaller::fillStatus(VacuulaStatus& s) const {
    if (haveVersion) {
        s.firmware = lastVersion.softwareVersion;
        s.model = lastVersion.modelName;
        s.serial = lastVersion.serialNumber;
        s.firmwareSupported = isSupportedFirmware(lastVersion.softwareVersion);
    }
    s.skeyReady = neato.hasSKey();
    const UploadProgress& p = neato.getUploadProgress();
    s.phase = uploadPhaseName(p.phase);
    s.bytesSent = static_cast<int>(p.sent);
    s.bytesTotal = static_cast<int>(p.total);
    s.message = p.message;
    s.resetRequested = resetRequested;
    s.resetSent = resetSent;
    s.resetError = resetError;
    s.quiet = neato.isQueuePaused();
    s.quietRemainingMs = static_cast<int>(neato.queuePausedRemainingMs());
    s.logNext = static_cast<int>(logNext);
}

void VacuulaInstaller::getStatus(std::function<void(bool, const VacuulaStatus&)> callback) {
    // While the UART is busy with the upload, about to reset, or deliberately
    // quiet, a GetVersion would fail or lag; report the last known identity.
    bool pendingReset = resetRequested && !resetSent && resetError.isEmpty();
    if (neato.isUploading() || neato.isQueuePaused() || pendingReset || haveVersion) {
        VacuulaStatus s;
        fillStatus(s);
        callback(true, s);
        return;
    }
    neato.getVersion([this, callback](bool ok, const VersionData& v) {
        if (ok) {
            lastVersion = v;
            haveVersion = true;
        }
        VacuulaStatus s;
        fillStatus(s);
        callback(true, s);
    });
}

void VacuulaInstaller::onConsoleLine(const String& line) {
    LOG("VACUULA", "robot: %s", line.c_str());
    Locked guard(lock);
    logLines.push_back({logNext++, line});
    while (logLines.size() > VACUULA_LOG_MAX_LINES)
        logLines.erase(logLines.begin());
}

String VacuulaInstaller::getLogJson(unsigned long since) {
    Locked guard(lock);
    String json;
    json.reserve(96 + logLines.size() * (VACUULA_LOG_LINE_MAX + 24));
    json += "{\"next\":";
    json += String(logNext);
    json += ",\"lines\":[";
    bool first = true;
    for (const LogLine& l: logLines) {
        if (l.seq < since)
            continue;
        if (!first)
            json += ',';
        first = false;
        json += "{\"seq\":";
        json += String(l.seq);
        json += ",\"text\":\"";
        json += jsonEscape(l.text);
        json += "\"}";
    }
    json += "]}";
    return json;
}
