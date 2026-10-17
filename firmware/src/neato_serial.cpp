#include "neato_serial.h"
#include "config.h"

// -- Lifecycle ---------------------------------------------------------------

// Helper: create a cache hit lambda that fires loggerCallback with cached=true.
// Captures `this` so it reads loggerCallback at call time (works before setLogger).
#define CACHE_HIT(CMD)                                                                                                 \
    [this](unsigned long ageMs) {                                                                                      \
        if (loggerCallback)                                                                                            \
            loggerCallback(CMD, CMD_SUCCESS, 0, "", 0, 0, ageMs);                                                      \
    }

NeatoSerial::NeatoSerial() :
    LoopTask(0), versionCache(
                         CACHE_TTL_VERSION, [this](AsyncCache<VersionData>::Callback cb) { fetchVersion(cb); },
                         CACHE_HIT(CMD_GET_VERSION)),
    chargerCache(
            CACHE_TTL_CHARGER, [this](AsyncCache<ChargerData>::Callback cb) { fetchCharger(cb); },
            CACHE_HIT(CMD_GET_CHARGER)),
    analogCache(
            CACHE_TTL_SENSORS, [this](AsyncCache<BatteryAnalogData>::Callback cb) { fetchBatteryAnalog(cb); },
            CACHE_HIT(CMD_GET_ANALOG_SENSORS)),
    warrantyCache(
            CACHE_TTL_VERSION, [this](AsyncCache<BatteryWarrantyData>::Callback cb) { fetchBatteryWarranty(cb); },
            CACHE_HIT(CMD_GET_WARRANTY)),
    digitalCache(
            CACHE_TTL_SENSORS, [this](AsyncCache<DigitalSensorData>::Callback cb) { fetchDigitalSensors(cb); },
            CACHE_HIT(CMD_GET_DIGITAL_SENSORS)),
    motorCache(
            CACHE_TTL_SENSORS, [this](AsyncCache<MotorData>::Callback cb) { fetchMotors(cb); },
            CACHE_HIT(CMD_GET_MOTORS)),
    stateCache(
            CACHE_TTL_STATE, [this](AsyncCache<RobotState>::Callback cb) { fetchState(cb); }, CACHE_HIT(CMD_GET_STATE)),
    errCache(
            CACHE_TTL_STATE, [this](AsyncCache<ErrorData>::Callback cb) { fetchErr(cb); }, CACHE_HIT(CMD_GET_ERR)),
    ldsCache(
            CACHE_TTL_LDS, [this](AsyncCache<LdsScanData>::Callback cb) { fetchLdsScan(cb); },
            CACHE_HIT(CMD_GET_LDS_SCAN)),
    robotPosRawCache(
            CACHE_TTL_SENSORS,
            [this](AsyncCache<RobotPosData>::Callback cb) { fetchRobotPos(CMD_GET_ROBOT_POS_RAW, cb); },
            CACHE_HIT(CMD_GET_ROBOT_POS_RAW)),
    robotPosSmoothCache(
            CACHE_TTL_SENSORS,
            [this](AsyncCache<RobotPosData>::Callback cb) { fetchRobotPos(CMD_GET_ROBOT_POS_SMOOTH, cb); },
            CACHE_HIT(CMD_GET_ROBOT_POS_SMOOTH)),
    userSettingsCache(
            CACHE_TTL_VERSION, [this](AsyncCache<UserSettingsData>::Callback cb) { fetchUserSettings(cb); },
            CACHE_HIT(CMD_GET_USER_SETTINGS)) {
    TaskRegistry::add(this);
}

#undef CACHE_HIT

void NeatoSerial::begin(int txPin, int rxPin) {
    uart.setRxBufferSize(NEATO_UART_RX_BUFFER);
    uart.begin(NEATO_BAUD_RATE, SERIAL_8N1, rxPin, txPin);
    LOG("NEATO", "UART initialized (TX=GPIO%d, RX=GPIO%d, baud=%d)", txPin, rxPin, NEATO_BAUD_RATE);
}

void NeatoSerial::initSKey() {
    sKeyPending = true;
    versionCache.invalidate(); // Force a fresh fetch (don't serve a stale failure)
    getVersion([this](bool ok, const VersionData& v) {
        if (!ok || v.serialNumber.length() == 0) {
            LOG("NEATO", "SKey init failed — GetVersion returned no serial, retrying in %lu ms", sKeyRetryDelay);
            sKeyRetryAt = millis() + sKeyRetryDelay;
            sKeyRetryDelay = (sKeyRetryDelay * 2 < SKEY_RETRY_MAX_MS) ? sKeyRetryDelay * 2 : SKEY_RETRY_MAX_MS;
            return;
        }
        sKeyPending = false;
        robotModelName = v.modelName;
        LOG("NEATO", "Model: %s (supported=%s)", robotModelName.c_str(),
            isSupportedModel(robotModelName) ? "yes" : "no");
        sKey = computeSKey(v.serialNumber);
        if (sKey.length() > 0) {
            LOG("NEATO", "SKey computed (%d chars) from serial %s", sKey.length(), v.serialNumber.c_str());
        } else {
            LOG("NEATO", "SKey computation failed for serial: %s", v.serialNumber.c_str());
        }
    });
}

String NeatoSerial::buildSetEvent(const char *event) const {
    return String(CMD_SET_EVENT_PREFIX) + event + CMD_SET_EVENT_SKEY + sKey;
}

void NeatoSerial::tick() {
    // Drive initSKey lifecycle: first attempt + retries on failure.
    // Runs inside tick() (not setup()) so the UART state machine is already
    // processing the queue — avoids the race where GetVersion was enqueued
    // in setup() but tick() hadn't started yet.
    if (sKeyPending && millis() >= sKeyRetryAt) {
        sKeyRetryAt = ULONG_MAX; // Prevent re-entry while fetch is in flight
        initSKey();
    }

    switch (state) {
        case QUEUE_IDLE:
            // A pending bulk upload takes the UART as soon as the queue drains.
            if (upload.phase == UPLOAD_PENDING) {
                startUpload();
                break;
            }
            // Quiet window: hold the queue, keep listening to the robot.
            if (queuePauseMs != 0) {
                if (millis() - queuePausedAt < queuePauseMs) {
                    readIdleLines();
                    break;
                }
                LOG("NEATO", "Quiet window over, resuming command queue");
                queuePauseMs = 0;
                dropQueued("quiet window ended");
            }
            if (!queue.empty()) {
                dequeueNext();
            }
            break;

        case QUEUE_UPLOAD_WAIT_REQ:
            tickUploadHandshake();
            break;

        case QUEUE_UPLOAD_STREAM:
            tickUploadStream();
            break;

        case QUEUE_UPLOAD_WAIT_ACK:
            tickUploadAck();
            break;

        case QUEUE_SENDING:
            sendCurrentCommand();
            break;

        case QUEUE_WAITING_RESPONSE: {
            // Read all available bytes
            while (uart.available()) {
                char c = static_cast<char>(uart.read());
                if (c == NEATO_RESPONSE_TERMINATOR) {
                    unsigned long elapsed = millis() - commandSentAt;
                    LOG("NEATO", "RX: %u bytes in %lu ms", responseBuffer.length(), elapsed);

                    // Validate: response must echo the sent command on the first line.
                    // If it doesn't, the UART stream is desynced (we got a response
                    // meant for a different command). Flush and fail this request so
                    // the next command starts clean.
                    if (!validateResponseEcho(responseBuffer)) {
                        LOG("NEATO", "DESYNC: sent '%s' but response echoed a different command, flushing",
                            currentCommand.c_str());
                        flushUartRx();
                        completeCommand(CMD_SERIAL_ERROR, responseBuffer);
                        return;
                    }

                    // Detect "Unknown Cmd" — robot doesn't support this command
                    if (responseBuffer.indexOf("Unknown Cmd") >= 0) {
                        LOG("NEATO", "Unsupported: %s", currentCommand.c_str());
                        completeCommand(CMD_UNSUPPORTED, responseBuffer);
                        return;
                    }

                    completeCommand(CMD_SUCCESS, responseBuffer);
                    return;
                }
                responseBuffer += c;
            }
            // Check timeout
            if (millis() - commandSentAt >= NEATO_CMD_TIMEOUT_MS) {
                LOG("NEATO", "Timeout: %s (%lu ms, partial: %u bytes)", currentCommand.c_str(),
                    (unsigned long) NEATO_CMD_TIMEOUT_MS, responseBuffer.length());
                // Log partial response on timeout (useful for debugging serial issues).
                // Flush UART to prevent stale bytes from leaking into the next command.
                flushUartRx();
                completeCommand(CMD_TIMEOUT, responseBuffer);
            }
            break;
        }

        case QUEUE_INTER_DELAY:
            if (millis() - delayStartedAt >= NEATO_INTER_CMD_DELAY_MS) {
                state = QUEUE_IDLE;
            }
            break;
    }
}

// -- Queue management --------------------------------------------------------

bool NeatoSerial::enqueue(const String& command, std::function<void(bool, const String&)> callback,
                          CommandPriority priority, bool waitForResponse) {
    // Fail fast during the quiet window instead of parking the command (and
    // its HTTP request) until the window ends.
    if (queuePauseMs != 0) {
        LOG("NEATO", "Queue paused, rejecting: %s", command.c_str());
        if (loggerCallback)
            loggerCallback(command, CMD_QUEUE_FULL, 0, "", static_cast<int>(queue.size()), 0, 0);
        if (callback)
            callback(false, "");
        return false;
    }
    if (static_cast<int>(queue.size()) >= NEATO_QUEUE_MAX_SIZE) {
        LOG("NEATO", "Queue full, rejecting: %s", command.c_str());
        if (loggerCallback)
            loggerCallback(command, CMD_QUEUE_FULL, 0, "", static_cast<int>(queue.size()), 0, 0);
        if (callback)
            callback(false, "");
        return false;
    }
    // Lower number = higher priority. Keep FIFO order within same priority.
    auto it = queue.begin();
    for (; it != queue.end(); ++it) {
        if (it->priority > priority)
            break;
    }
    queue.insert(it, {command, static_cast<uint8_t>(priority), callback, waitForResponse});
    return true;
}

std::function<void(bool, const String&)> NeatoSerial::wrapAction(std::function<void(bool)> callback) {
    if (!callback)
        return nullptr;
    return [callback](bool ok, const String&) { callback(ok); };
}

void NeatoSerial::dequeueNext() {
    if (queue.empty())
        return;

    // Capture queue depth before dequeue (for logging)
    queueDepthAtStart = static_cast<int>(queue.size());

    CommandEntry entry = queue.front();
    queue.erase(queue.begin());

    currentCommand = entry.command;
    currentCallback = entry.callback;
    currentWaitForResponse = entry.waitForResponse;
    responseBuffer = "";

    state = QUEUE_SENDING;
}

void NeatoSerial::flushUartRx() {
    int flushed = 0;
    while (uart.available()) {
        uart.read();
        flushed++;
    }
    if (flushed > 0) {
        LOG("NEATO", "Flushed %d stale bytes from UART RX", flushed);
    }
}

void NeatoSerial::sendCurrentCommand() {
    // Drain any stale bytes from a previous response that may have arrived late
    flushUartRx();

    LOG("NEATO", "TX: %s", currentCommand.c_str());
    uart.print(currentCommand + "\n");
    commandSentAt = millis();
    if (!currentWaitForResponse) {
        completeCommand(CMD_SUCCESS, "");
        return;
    }
    state = QUEUE_WAITING_RESPONSE;
}

bool NeatoSerial::validateResponseEcho(const String& response) const {
    // The Neato echoes the command on the first line of the response,
    // e.g. "GetCharger\r\nLabel,Value\r\n...". Extract the first word of the
    // sent command (before any space/flag) and check the response starts with it.
    // Some commands echo only the name ("Clean" for "Clean House"), while others
    // echo the full command with arguments ("TestMode On" for "TestMode On").
    // Using startsWith handles both cases.
    String expectedEcho = currentCommand;
    int spacePos = expectedEcho.indexOf(' ');
    if (spacePos > 0)
        expectedEcho = expectedEcho.substring(0, spacePos);

    // Find the first line in the response (up to \r or \n)
    String firstLine = response;
    int crPos = firstLine.indexOf('\r');
    int lfPos = firstLine.indexOf('\n');
    int lineEnd = -1;
    if (crPos >= 0 && lfPos >= 0)
        lineEnd = (crPos < lfPos) ? crPos : lfPos;
    else if (crPos >= 0)
        lineEnd = crPos;
    else if (lfPos >= 0)
        lineEnd = lfPos;
    if (lineEnd >= 0)
        firstLine = firstLine.substring(0, lineEnd);

    // Check that the echo line starts with the expected command name (case-insensitive).
    firstLine.trim();
    expectedEcho.trim();
    firstLine.toLowerCase();
    expectedEcho.toLowerCase();
    return firstLine.startsWith(expectedEcho);
}

void NeatoSerial::completeCommand(CommandStatus status, const String& response) {
    unsigned long elapsed = millis() - commandSentAt;
    String cmd = currentCommand;
    auto cb = currentCallback;
    int qDepth = queueDepthAtStart;
    String resp = response; // Copy before clearing responseBuffer (response is a ref to it)
    size_t respBytes = resp.length();

    currentCommand = "";
    currentCallback = nullptr;
    currentWaitForResponse = true;
    responseBuffer = "";
    queueDepthAtStart = 0;

    // Start inter-command delay before next command
    delayStartedAt = millis();
    state = QUEUE_INTER_DELAY;

    // Fire logger hook before user callback with enhanced metadata
    if (loggerCallback)
        loggerCallback(cmd, status, elapsed, resp, qDepth, respBytes, 0);

    // User callback still gets simple bool for backward compatibility
    bool success = (status == CMD_SUCCESS);
    if (cb)
        cb(success, resp);
}

// -- Cached sensor query methods (public API) --------------------------------
// These delegate to AsyncCache, which handles TTL, dedup, and coalescing.

void NeatoSerial::getVersion(std::function<void(bool, const VersionData&)> callback) {
    versionCache.get(callback);
}

void NeatoSerial::getCharger(std::function<void(bool, const ChargerData&)> callback) {
    chargerCache.get(callback);
}

void NeatoSerial::getBatteryAnalog(std::function<void(bool, const BatteryAnalogData&)> callback) {
    analogCache.get(callback);
}

void NeatoSerial::getBatteryWarranty(std::function<void(bool, const BatteryWarrantyData&)> callback) {
    warrantyCache.get(callback);
}

void NeatoSerial::getUserSettings(std::function<void(bool, const UserSettingsData&)> callback) {
    userSettingsCache.get(callback);
}

void NeatoSerial::getDigitalSensors(std::function<void(bool, const DigitalSensorData&)> callback) {
    getDigitalSensors(callback, PRIORITY_NORMAL);
}

void NeatoSerial::getDigitalSensors(std::function<void(bool, const DigitalSensorData&)> callback,
                                    CommandPriority priority) {
    // Normal priority goes through cache; elevated priority bypasses it
    // to guarantee commands are enqueued at the requested priority.
    if (priority == PRIORITY_NORMAL) {
        digitalCache.get(callback);
    } else {
        fetchDigitalSensors(callback, priority);
    }
}

void NeatoSerial::getMotors(std::function<void(bool, const MotorData&)> callback) {
    getMotors(callback, PRIORITY_NORMAL);
}

void NeatoSerial::getMotors(std::function<void(bool, const MotorData&)> callback, CommandPriority priority) {
    // Normal priority goes through cache; elevated priority bypasses it
    // to guarantee commands are enqueued at the requested priority.
    if (priority == PRIORITY_NORMAL) {
        motorCache.get(callback);
    } else {
        fetchMotors(callback, priority);
    }
}

void NeatoSerial::getState(std::function<void(bool, const RobotState&)> callback) {
    // During manual clean, the robot reports UIMGR_STATE_TESTMODE.
    // Override to UIMGR_STATE_MANUALCLEANING so callers (frontend, dashboard)
    // see the correct pseudo-state without needing to know about TestMode internals.
    if (manualCleanActive) {
        stateCache.get([callback](bool ok, const RobotState& data) {
            if (!ok || !callback) {
                if (callback)
                    callback(ok, data);
                return;
            }
            RobotState patched = data;
            patched.uiState = "UIMGR_STATE_MANUALCLEANING";
            callback(true, patched);
        });
        return;
    }
    stateCache.get(callback);
}

void NeatoSerial::getErr(std::function<void(bool, const ErrorData&)> callback) {
    errCache.get(callback);
}

void NeatoSerial::getErrClear(std::function<void(bool, const ErrorData&)> callback) {
    // getErrClear is never cached — it clears the error and always needs a fresh fetch
    fetchErrClear(callback);
}

void NeatoSerial::getLdsScan(std::function<void(bool, const LdsScanData&)> callback) {
    ldsCache.getStaleWhileRefreshing(callback);
}

void NeatoSerial::getRobotPos(bool smooth, std::function<void(bool, const RobotPosData&)> callback) {
    (smooth ? robotPosSmoothCache : robotPosRawCache).get(callback);
}

// -- Raw fetch methods (enqueue serial command, parse response) ---------------

void NeatoSerial::fetchVersion(std::function<void(bool, const VersionData&)> callback) {
    enqueue(CMD_GET_VERSION, [callback](bool ok, const String& raw) {
        VersionData data;
        if (ok)
            ok = parseVersionData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchCharger(std::function<void(bool, const ChargerData&)> callback) {
    enqueue(CMD_GET_CHARGER, [callback](bool ok, const String& raw) {
        ChargerData data;
        if (ok)
            ok = parseChargerData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchBatteryAnalog(std::function<void(bool, const BatteryAnalogData&)> callback) {
    enqueue(CMD_GET_ANALOG_SENSORS, [callback](bool ok, const String& raw) {
        BatteryAnalogData data;
        if (ok)
            ok = parseBatteryAnalogData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchBatteryWarranty(std::function<void(bool, const BatteryWarrantyData&)> callback) {
    enqueue(CMD_GET_WARRANTY, [callback](bool ok, const String& raw) {
        BatteryWarrantyData data;
        if (ok)
            ok = parseBatteryWarrantyData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchDigitalSensors(std::function<void(bool, const DigitalSensorData&)> callback,
                                      CommandPriority priority) {
    enqueue(
            CMD_GET_DIGITAL_SENSORS,
            [callback](bool ok, const String& raw) {
                DigitalSensorData data;
                if (ok)
                    ok = parseDigitalSensorData(raw, data);
                if (callback)
                    callback(ok, data);
            },
            priority);
}

void NeatoSerial::fetchMotors(std::function<void(bool, const MotorData&)> callback, CommandPriority priority) {
    enqueue(
            CMD_GET_MOTORS,
            [callback](bool ok, const String& raw) {
                MotorData data;
                if (ok)
                    ok = parseMotorData(raw, data);
                if (callback)
                    callback(ok, data);
            },
            priority);
}

void NeatoSerial::fetchState(std::function<void(bool, const RobotState&)> callback) {
    enqueue(CMD_GET_STATE, [callback](bool ok, const String& raw) {
        RobotState data;
        if (ok)
            ok = parseRobotState(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchErr(std::function<void(bool, const ErrorData&)> callback) {
    enqueue(CMD_GET_ERR, [callback](bool ok, const String& raw) {
        ErrorData data;
        if (ok)
            ok = parseErrorData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchErrClear(std::function<void(bool, const ErrorData&)> callback) {
    enqueue(CMD_GET_ERR_CLEAR, [callback](bool ok, const String& raw) {
        ErrorData data;
        if (ok)
            ok = parseErrorData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchLdsScan(std::function<void(bool, const LdsScanData&)> callback) {
    enqueue(CMD_GET_LDS_SCAN, [callback](bool ok, const String& raw) {
        LdsScanData data;
        if (ok)
            ok = parseLdsScanData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchRobotPos(const char *cmd, std::function<void(bool, const RobotPosData&)> callback) {
    enqueue(cmd, [callback](bool ok, const String& raw) {
        RobotPosData data;
        if (ok)
            ok = parseRobotPosData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

void NeatoSerial::fetchUserSettings(std::function<void(bool, const UserSettingsData&)> callback) {
    enqueue(CMD_GET_USER_SETTINGS, [callback](bool ok, const String& raw) {
        UserSettingsData data;
        if (ok)
            ok = parseUserSettingsData(raw, data);
        if (callback)
            callback(ok, data);
    });
}

// -- Cache invalidation ------------------------------------------------------

void NeatoSerial::invalidateState() {
    stateCache.invalidate();
    errCache.invalidate();
}

void NeatoSerial::invalidateAll() {
    versionCache.invalidate();
    chargerCache.invalidate();
    analogCache.invalidate();
    warrantyCache.invalidate();
    digitalCache.invalidate();
    motorCache.invalidate();
    stateCache.invalidate();
    errCache.invalidate();
    ldsCache.invalidate();
    robotPosRawCache.invalidate();
    robotPosSmoothCache.invalidate();
    userSettingsCache.invalidate();
}

// -- Action command convenience methods --------------------------------------

bool NeatoSerial::clean(const String& action, std::function<void(bool)> callback) {
    // All cleaning control uses SetEvent — the authenticated event API that D3-D7
    // robots use for their cloud/app protocol. This correctly transitions the UI
    // state machine and preserves map/localization during pause/resume.
    //
    // SKey must be computed at boot via initSKey(). If missing, commands will fail
    // gracefully (callback with false).

    if (!hasSKey()) {
        LOG("NEATO", "clean(%s) failed — SKey not available", action.c_str());
        if (callback)
            callback(false);
        return false;
    }

    if (action == "dock") {
        invalidateState();
        return enqueue(buildSetEvent(EVT_SEND_TO_BASE), wrapAction(callback), PRIORITY_HIGH);
    }

    if (action == "pause") {
        invalidateState();
        return enqueue(buildSetEvent(EVT_PAUSE), wrapAction(callback), PRIORITY_HIGH);
    }

    if (action == "stop") {
        invalidateState();
        return enqueue(buildSetEvent(EVT_STOP), wrapAction(callback), PRIORITY_HIGH);
    }

    // Type-aware resume: "house" only resumes a paused house clean, "spot"
    // only resumes a paused spot clean. Prevents HA "start" from resuming
    // a previously paused spot clean when the user expects a new house clean.
    if (action == "house") {
        bool isPausedHouse =
                stateCache.hasCached() && stateCache.getCached().uiState.indexOf("HOUSECLEANINGPAUSED") >= 0;
        if (isPausedHouse) {
            invalidateState();
            return enqueue(buildSetEvent(EVT_RESUME), wrapAction(callback), PRIORITY_HIGH);
        }
        invalidateState();
        if (cleanStartCallback)
            cleanStartCallback();
        // Send navigation mode first (fire-and-forget); house clean proceeds regardless.
        if (navModeGetter) {
            String mode = navModeGetter();
            if (mode.length() > 0 && mode != "Normal") {
                String navCmd = String(CMD_SET_NAVIGATION_MODE) + " " + mode;
                enqueue(navCmd, nullptr, PRIORITY_HIGH);
            }
        }
        return enqueue(buildSetEvent(EVT_START_HOUSE), wrapAction(callback), PRIORITY_HIGH);
    }

    if (action == "spot") {
        bool isPausedSpot = stateCache.hasCached() && stateCache.getCached().uiState.indexOf("SPOTCLEANINGPAUSED") >= 0;
        if (isPausedSpot) {
            invalidateState();
            return enqueue(buildSetEvent(EVT_RESUME), wrapAction(callback), PRIORITY_HIGH);
        }
        invalidateState();
        if (cleanStartCallback)
            cleanStartCallback();
        return enqueue(buildSetEvent(EVT_START_SPOT), wrapAction(callback), PRIORITY_HIGH);
    }

    // Generic action — resume whatever is paused, or start house clean
    bool isPaused = stateCache.hasCached() && stateCache.getCached().uiState.indexOf("CLEANINGPAUSED") >= 0;
    if (isPaused) {
        invalidateState();
        return enqueue(buildSetEvent(EVT_RESUME), wrapAction(callback), PRIORITY_HIGH);
    }

    // New clean from idle — fall through to house clean
    invalidateState();
    if (cleanStartCallback)
        cleanStartCallback();
    // Send navigation mode first (fire-and-forget); house clean proceeds regardless.
    if (navModeGetter) {
        String mode = navModeGetter();
        if (mode.length() > 0 && mode != "Normal") {
            String navCmd = String(CMD_SET_NAVIGATION_MODE) + " " + mode;
            enqueue(navCmd, nullptr, PRIORITY_HIGH);
        }
    }
    return enqueue(buildSetEvent(EVT_START_HOUSE), wrapAction(callback), PRIORITY_HIGH);
}

bool NeatoSerial::testMode(bool enable, std::function<void(bool)> callback) {
    const char *cmd = enable ? CMD_TEST_MODE_ON : CMD_TEST_MODE_OFF;
    invalidateState();
    // HIGH priority: TestMode is a prerequisite for motor commands — must execute
    // before any CRITICAL/MEDIUM motor commands that may already be queued.
    return enqueue(cmd, wrapAction(callback), PRIORITY_HIGH);
}

bool NeatoSerial::playSound(SoundId soundId, std::function<void(bool)> callback) {
    String cmd = String(CMD_PLAY_SOUND) + " SoundID " + String(static_cast<int>(soundId));
    return enqueue(cmd, wrapAction(callback));
}

bool NeatoSerial::setLdsRotation(bool on, std::function<void(bool)> callback) {
    const char *cmd = on ? CMD_SET_LDS_ROTATION_ON : CMD_SET_LDS_ROTATION_OFF;
    // HIGH priority: LDS rotation is a setup/teardown command that must execute
    // before motor commands during manual clean enable/disable sequences.
    return enqueue(cmd, wrapAction(callback), PRIORITY_HIGH);
}

bool NeatoSerial::setMotorWheels(int leftMM, int rightMM, int speedMMs, std::function<void(bool)> callback) {
    // SetMotor 0 0 0 bug: zero values are ignored, robot coasts for ~1s.
    // Workaround: disable wheels for immediate stop, then re-enable for next command.
    if (leftMM == 0 && rightMM == 0) {
        return enqueue(
                String(CMD_SET_MOTOR) + " LWheelDisable RWheelDisable",
                [this, callback](bool ok, const String&) {
                    // Re-enable wheels so subsequent move commands work
                    enqueue(String(CMD_SET_MOTOR) + " LWheelEnable RWheelEnable", wrapAction(callback),
                            PRIORITY_CRITICAL);
                },
                PRIORITY_CRITICAL);
    }
    // Robot rejects distances outside ±10000mm — clamp to protocol limits
    leftMM = constrain(leftMM, -10000, 10000);
    rightMM = constrain(rightMM, -10000, 10000);
    speedMMs = constrain(speedMMs, 0, 300);
    String cmd = String(CMD_SET_MOTOR) + " LWheelDist " + String(leftMM) + " RWheelDist " + String(rightMM) +
                 " Speed " + String(speedMMs);
    return enqueue(cmd, wrapAction(callback), PRIORITY_CRITICAL);
}

bool NeatoSerial::setMotorBrush(int rpm, std::function<void(bool)> callback) {
    if (rpm <= 0) {
        return enqueue(String(CMD_SET_MOTOR) + " BrushDisable", wrapAction(callback), PRIORITY_MEDIUM);
    }
    // BrushEnable energizes the motor driver, then Brush + RPM starts spinning.
    // Brush flag is mutually exclusive with wheel/vacuum commands per call.
    return enqueue(
            String(CMD_SET_MOTOR) + " BrushEnable",
            [this, rpm, callback](bool ok, const String&) {
                if (!ok) {
                    if (callback)
                        callback(false);
                    return;
                }
                enqueue(String(CMD_SET_MOTOR) + " Brush RPM " + String(rpm), wrapAction(callback), PRIORITY_MEDIUM);
            },
            PRIORITY_MEDIUM);
}

bool NeatoSerial::setMotorVacuum(bool on, int speedPercent, std::function<void(bool)> callback) {
    // VacuumSpeed must be combined with VacuumOn in the same call.
    String cmd = String(CMD_SET_MOTOR);
    cmd += on ? " VacuumOn VacuumSpeed " + String(speedPercent) : " VacuumOff";
    return enqueue(cmd, wrapAction(callback), PRIORITY_MEDIUM);
}

bool NeatoSerial::setMotorSideBrush(bool on, int powerMw, std::function<void(bool)> callback) {
    // Side brush (Botvac D3-D7 only) uses open-loop power control in milliwatts.
    // Two-layer control: SideBrushEnable energizes motor driver, SideBrushOn starts spinning.
    // Completely independent from the main brush.
    if (!on) {
        return enqueue(
                String(CMD_SET_MOTOR) + " SideBrushOff",
                [this, callback](bool ok, const String&) {
                    // Best-effort disable after stopping
                    enqueue(String(CMD_SET_MOTOR) + " SideBrushDisable", wrapAction(callback), PRIORITY_MEDIUM);
                },
                PRIORITY_MEDIUM);
    }
    return enqueue(
            String(CMD_SET_MOTOR) + " SideBrushEnable",
            [this, powerMw, callback](bool ok, const String&) {
                if (!ok) {
                    if (callback)
                        callback(false);
                    return;
                }
                enqueue(String(CMD_SET_MOTOR) + " SideBrushOn SideBrushPower " + String(powerMw), wrapAction(callback),
                        PRIORITY_MEDIUM);
            },
            PRIORITY_MEDIUM);
}

// -- User settings -----------------------------------------------------------

bool NeatoSerial::setUserSetting(const String& key, const String& value, std::function<void(bool)> callback) {
    userSettingsCache.invalidate();
    String cmd = String(CMD_SET_USER_SETTINGS) + " " + key + " " + value;
    return enqueue(cmd, wrapAction(callback));
}

bool NeatoSerial::setNavigationMode(const String& mode, std::function<void(bool)> callback) {
    String cmd = String(CMD_SET_NAVIGATION_MODE) + " " + mode;
    return enqueue(cmd, wrapAction(callback));
}

// -- Power control -----------------------------------------------------------

bool NeatoSerial::powerControl(const String& action, std::function<void(bool)> callback) {
    const char *cmd;
    if (action == "restart") {
        cmd = CMD_SET_SYSTEM_MODE_POWER_CYCLE;
    } else if (action == "shutdown") {
        cmd = CMD_SET_SYSTEM_MODE_SHUTDOWN;
    } else {
        LOG("NEATO", "powerControl: unknown action '%s'", action.c_str());
        if (callback)
            callback(false);
        return false;
    }

    // SetSystemMode requires TestMode On first (protocol specifies 100ms delay,
    // which the inter-command delay covers). Chain: TestMode On -> SetSystemMode.
    invalidateState();
    const char *sysCmd = cmd; // capture for lambda
    return testMode(true, [this, sysCmd, callback](bool ok) {
        if (!ok) {
            LOG("NEATO", "powerControl: TestMode On failed");
            if (callback)
                callback(false);
            return;
        }
        enqueue(sysCmd, wrapAction(callback), PRIORITY_HIGH, false);
    });
}

// -- Clear errors ------------------------------------------------------------

bool NeatoSerial::clearErrors(std::function<void(bool)> callback) {
    invalidateState();
    return enqueue(CMD_SET_UI_ERROR_CLEAR_ALL, wrapAction(callback));
}

bool NeatoSerial::newBattery(std::function<void(bool)> callback) {
    invalidateState();
    return testMode(true, [this, callback](bool ok) {
        if (!ok) {
            if (callback)
                callback(false);
            return;
        }

        enqueue(
                CMD_NEW_BATTERY,
                [this, callback](bool okCmd, const String&) {
                    if (!okCmd) {
                        // Best-effort return robot to normal mode even if NewBattery failed
                        testMode(false, nullptr);
                        if (callback)
                            callback(false);
                        return;
                    }
                    testMode(false, callback);
                },
                PRIORITY_HIGH);
    });
}

bool NeatoSerial::sendRaw(const String& cmd, std::function<void(bool, const String&)> callback) {
    if (cmd.isEmpty())
        return false;
    return enqueue(cmd, callback);
}

// -- Bulk file upload --------------------------------------------------------

const char *uploadPhaseName(UploadPhase phase) {
    switch (phase) {
        case UPLOAD_IDLE:
            return "idle";
        case UPLOAD_PENDING:
            return "pending";
        case UPLOAD_HANDSHAKE:
            return "handshake";
        case UPLOAD_SENDING:
            return "sending";
        case UPLOAD_AWAITING_ACK:
            return "awaiting_ack";
        case UPLOAD_DONE:
            return "done";
        case UPLOAD_FAILED:
            return "failed";
    }
    return "unknown";
}

bool NeatoSerial::uploadFile(const String& kind, std::vector<uint8_t>&& data,
                             std::function<void(bool, const String&)> callback) {
    if (isUploading()) {
        LOG("NEATO", "Upload rejected: another transfer is in flight (%s)", uploadPhaseName(upload.phase));
        return false;
    }
    if (kind.isEmpty() || data.empty())
        return false;

    uploadData = std::move(data);
    // The announced size includes the trailing checksum, as Neato's tool does.
    uploadCommand =
            String(CMD_UPLOAD_PREFIX) + kind + CMD_UPLOAD_SIZE + String(uploadData.size() + UPLOAD_CHECKSUM_BYTES);
    uploadCallback = callback;
    upload = UploadProgress();
    upload.phase = UPLOAD_PENDING;
    upload.total = uploadData.size();
    LOG("NEATO", "Upload queued: %s (%u bytes)", uploadCommand.c_str(), upload.total);
    return true;
}

void NeatoSerial::startUpload() {
    flushUartRx();
    LOG("NEATO", "TX: %s", uploadCommand.c_str());
    uart.print(uploadCommand + "\n");
    uploadStartedAt = millis();
    uploadPhaseStartedAt = uploadStartedAt;
    uploadRxText = "";
    uploadBytesSinceGap = 0;
    upload.sent = 0;
    upload.phase = UPLOAD_HANDSHAKE;
    state = QUEUE_UPLOAD_WAIT_REQ;
}

void NeatoSerial::collectUploadByte(char c) {
    if (c < 0x20 || c > 0x7E)
        return;
    if (uploadRxText.length() < UPLOAD_RX_TEXT_MAX)
        uploadRxText += c;
}

void NeatoSerial::tickUploadHandshake() {
    while (uart.available()) {
        char c = static_cast<char>(uart.read());
        if (c == UPLOAD_REQ_BYTE) {
            LOG("NEATO", "Upload: robot ready (REQ after %lu ms), streaming %u bytes", millis() - uploadPhaseStartedAt,
                upload.total);
            upload.phase = UPLOAD_SENDING;
            uploadLastWriteAt = millis();
            state = QUEUE_UPLOAD_STREAM;
            return;
        }
        collectUploadByte(c);
        // The robot echoes the command and reports an unknown/invalid one as text.
        if (uploadRxText.indexOf("Unknown Cmd") >= 0) {
            finishUpload(false, "robot does not support the Upload command");
            return;
        }
    }
    if (millis() - uploadPhaseStartedAt >= UPLOAD_REQ_TIMEOUT_MS) {
        finishUpload(false, "robot did not request the upload (got: " + uploadRxText + ")");
    }
}

void NeatoSerial::tickUploadStream() {
    // Nothing is expected from the robot during a one-shot blast, but a NAK
    // here means it gave up on us — stop pushing bytes into the void.
    while (uart.available()) {
        char c = static_cast<char>(uart.read());
        if (c == UPLOAD_NAK_BYTE) {
            finishUpload(false, "robot rejected the transfer mid-stream");
            return;
        }
    }

    // Short breather after every chunk, mirroring Neato's upload tool.
    if (uploadBytesSinceGap >= UPLOAD_CHUNK_BYTES) {
        if (millis() - uploadLastWriteAt < UPLOAD_CHUNK_GAP_MS)
            return;
        uploadBytesSinceGap = 0;
    }

    size_t remaining = uploadData.size() - upload.sent;
    if (remaining > 0) {
        int space = uart.availableForWrite();
        if (space <= 0)
            return;
        size_t n = remaining;
        if (n > static_cast<size_t>(space))
            n = static_cast<size_t>(space);
        if (n > UPLOAD_CHUNK_BYTES - uploadBytesSinceGap)
            n = UPLOAD_CHUNK_BYTES - uploadBytesSinceGap;
        size_t written = uart.write(uploadData.data() + upload.sent, n);
        upload.sent += written;
        uploadBytesSinceGap += written;
        uploadLastWriteAt = millis();
        return;
    }

    // Payload done: trailing little-endian 32-bit sum of every payload byte.
    uint32_t sum = 0;
    for (uint8_t b: uploadData)
        sum += b;
    uint8_t checksum[UPLOAD_CHECKSUM_BYTES] = {
            static_cast<uint8_t>(sum & 0xFF), static_cast<uint8_t>((sum >> 8) & 0xFF),
            static_cast<uint8_t>((sum >> 16) & 0xFF), static_cast<uint8_t>((sum >> 24) & 0xFF)};
    uart.write(checksum, UPLOAD_CHECKSUM_BYTES);
    uart.flush();
    LOG("NEATO", "Upload: %u bytes sent in %lu ms, checksum 0x%08lX, waiting for ACK", upload.sent,
        millis() - uploadStartedAt, static_cast<unsigned long>(sum));
    uploadRxText = "";
    uploadPhaseStartedAt = millis();
    upload.phase = UPLOAD_AWAITING_ACK;
    state = QUEUE_UPLOAD_WAIT_ACK;
}

void NeatoSerial::tickUploadAck() {
    while (uart.available()) {
        char c = static_cast<char>(uart.read());
        if (c == UPLOAD_ACK_BYTE) {
            finishUpload(true, "accepted");
            return;
        }
        if (c == UPLOAD_NAK_BYTE) {
            finishUpload(false, "robot rejected the upload (checksum or size mismatch)");
            return;
        }
        collectUploadByte(c);
    }
    if (millis() - uploadPhaseStartedAt >= UPLOAD_ACK_TIMEOUT_MS) {
        finishUpload(false, "no acknowledgement from robot (got: " + uploadRxText + ")");
    }
}

void NeatoSerial::finishUpload(bool ok, const String& message) {
    unsigned long elapsed = millis() - uploadStartedAt;
    LOG("NEATO", "Upload %s after %lu ms: %s", ok ? "OK" : "FAILED", elapsed, message.c_str());

    std::vector<uint8_t>().swap(uploadData); // Release the buffer, not just clear it
    upload.phase = ok ? UPLOAD_DONE : UPLOAD_FAILED;
    upload.message = message;
    String cmd = uploadCommand;
    auto cb = uploadCallback;
    uploadCallback = nullptr;

    // Same post-command spacing as a regular command before the queue resumes.
    delayStartedAt = millis();
    state = QUEUE_INTER_DELAY;

    if (loggerCallback)
        loggerCallback(cmd, ok ? CMD_SUCCESS : CMD_SERIAL_ERROR, elapsed, message, static_cast<int>(queue.size()),
                       upload.total, 0);
    if (cb)
        cb(ok, message);
}

bool NeatoSerial::resetNetwork(std::function<void(bool)> callback) {
    if (!hasSKey())
        return false;
    return enqueue(String(CMD_SET_WIFI_RESET_NETWORK) + sKey, wrapAction(callback), PRIORITY_CRITICAL, false);
}

// -- Quiet window ------------------------------------------------------------

void NeatoSerial::dropQueued(const char *why) {
    if (queue.empty())
        return;
    std::vector<CommandEntry> dropped;
    dropped.swap(queue);
    LOG("NEATO", "Dropping %u queued command(s): %s", dropped.size(), why);
    for (CommandEntry& e: dropped) {
        if (loggerCallback)
            loggerCallback(e.command, CMD_QUEUE_FULL, 0, "", 0, 0, 0);
        if (e.callback)
            e.callback(false, "");
    }
}

void NeatoSerial::pauseQueue(unsigned long ms) {
    queuePausedAt = millis();
    queuePauseMs = ms != 0 ? ms : 1; // 0 means "not paused"
    idleLineBuffer = "";
    dropQueued("quiet window started");
    LOG("NEATO", "Command queue paused for %lu ms", ms);
}

void NeatoSerial::resumeQueue() {
    if (queuePauseMs == 0)
        return;
    queuePauseMs = 0;
    dropQueued("quiet window ended early");
    LOG("NEATO", "Command queue resumed");
}

unsigned long NeatoSerial::queuePausedRemainingMs() const {
    if (queuePauseMs == 0)
        return 0;
    unsigned long elapsed = millis() - queuePausedAt;
    return elapsed < queuePauseMs ? queuePauseMs - elapsed : 0;
}

void NeatoSerial::readIdleLines() {
    while (uart.available()) {
        char c = static_cast<char>(uart.read());
        if (c == '\n' || c == '\r' || c == NEATO_RESPONSE_TERMINATOR) {
            if (idleLineBuffer.length() > 0) {
                if (idleLineCallback)
                    idleLineCallback(idleLineBuffer);
                idleLineBuffer = "";
            }
            continue;
        }
        if (c < 0x20 || c > 0x7E)
            continue;
        if (idleLineBuffer.length() < VACUULA_LOG_LINE_MAX)
            idleLineBuffer += c;
    }
}
