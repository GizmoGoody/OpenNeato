#ifndef VACUULA_INSTALLER_H
#define VACUULA_INSTALLER_H

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <functional>
#include <vector>
#include "config.h"
#include "json_fields.h"
#include "neato_commands.h"

class NeatoSerial;
class DataLogger;

// Snapshot returned by GET /api/vacuula/status.
struct VacuulaStatus : public JsonSerializable {
    String firmware; // Robot software version from GetVersion (e.g. "4.5.3.189.0")
    String model;
    String serial; // Raw "Serial Number" field; the MAC is its second comma-separated part
    bool firmwareSupported = false;
    bool skeyReady = false;
    String phase; // Upload phase (idle/pending/handshake/sending/awaiting_ack/done/failed)
    int bytesSent = 0;
    int bytesTotal = 0;
    String message; // Upload outcome text
    bool resetRequested = false;
    bool resetSent = false;
    String resetError; // Why the reset could not be sent, if it could not
    bool quiet = false; // Robot polling paused (quiet window after the network reset)
    int quietRemainingMs = 0;
    int logNext = 0; // Sequence number the next captured console line will get

    std::vector<Field> toFields() const override;
};

// Hands a Vacuula install archive to the robot over the debug UART.
//
// The Vacuula project (github.com/vacuula) replaces Neato's cloud with a LAN
// server. On D3-D7 its installer uploads a small tar.gz (its scripts, the
// server certificate and address) with the robot's own "Upload Tarball"
// serial command, then resets the robot's WiFi so it comes up in setup mode;
// the rest of the provisioning happens over that setup network from a PC.
// This class covers the serial half: receive the archive over HTTP, gate on
// a firmware version the scripts know, run the upload, send the reset, then
// keep the command queue quiet for a while and capture the robot's console
// output so the PC side can follow the install. See docs/vacuula.md.
//
// Threading: HTTP handlers and body callbacks run on the async TCP task while
// NeatoSerial callbacks run on the Arduino loop task, so the archive, the
// receive bookkeeping and the console ring are guarded by a mutex. The mutex
// is never held across a NeatoSerial call (its callbacks may run inline).
class VacuulaInstaller {
public:
    VacuulaInstaller(NeatoSerial& neato, DataLogger& logger);
    void begin();

    // -- Archive reception (fed by the HTTP body callback) ---------------------
    // `owner` identifies the request the body belongs to, so a second POST
    // cannot interleave with or reuse another request's archive.
    bool beginReceive(const void *owner, size_t total);
    void receiveChunk(const void *owner, const uint8_t *data, size_t len);
    bool receiveComplete(const void *owner);
    String getReceiveError();

    // -- Actions ---------------------------------------------------------------
    // Validate the robot (model + firmware unless `force`, never during a
    // manual clean), hand the received archive to it and, when `reset` is set,
    // follow up with the network reset and the quiet window. The callback
    // fires once the upload has been queued or refused; progress is reported
    // through getStatus(). A refusal discards the received archive.
    void start(bool reset, bool force, std::function<void(bool, const String&)> callback);

    // Send the network reset on its own (e.g. a retry). Opens the quiet window.
    bool resetNetwork(String& error);

    // (Re)start the quiet window for `ms` milliseconds, keeping captured lines.
    void armQuiet(unsigned long ms);

    // End the quiet window early and resume robot polling.
    void endQuiet();

    // -- Reporting -------------------------------------------------------------
    void getStatus(std::function<void(bool, const VacuulaStatus&)> callback);

    // Console lines captured during the quiet window, as
    // {"next":N,"lines":[{"seq":n,"text":"..."}]}; `since` filters by seq.
    String getLogJson(unsigned long since);

    // True for the robot firmware builds Vacuula's Gen3 scripts know how to patch.
    static bool isSupportedFirmware(const String& softwareVersion);

private:
    struct LogLine {
        unsigned long seq;
        String text;
    };

    NeatoSerial& neato;
    DataLogger& dataLogger;
    SemaphoreHandle_t lock;

    // Guarded by `lock`
    std::vector<uint8_t> archive;
    const void *receiveOwner = nullptr;
    size_t expectedBytes = 0;
    bool receiveDone = false;
    String receiveError;
    bool startPending = false; // start() issued GetVersion, archive not yet handed over
    std::vector<LogLine> logLines;
    unsigned long logNext = 0;

    // Written on the loop task only
    bool resetRequested = false;
    bool resetSent = false;
    String resetError;
    VersionData lastVersion;
    bool haveVersion = false;

    void onUploadDone(bool ok, const String& message);
    bool sendReset();
    void onConsoleLine(const String& line);
    void fillStatus(VacuulaStatus& s) const;
};

#endif // VACUULA_INSTALLER_H
