#include <algorithm>
#include <cctype>
#include <cstring>
#include <deque>
#include <sstream>
#include <string>
#include <vector>

#ifdef IBM
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
using socket_len_t = int;
using socket_t = SOCKET;
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
using socket_len_t = socklen_t;
using socket_t = int;
#define INVALID_SOCKET (-1)
#define SOCKET_ERROR (-1)
#endif

#if IBM
#include <windows.h>
#endif
#ifdef __APPLE__
#include <OpenGL/gl.h>
#else
#include <GL/gl.h>
#endif

#include "XPLMDisplay.h"
#include "XPLMGraphics.h"
#include "XPLMMenus.h"
#include "XPLMPlugin.h"
#include "XPLMProcessing.h"
#include "XPLMUtilities.h"
#include "XPStandardWidgets.h"
#include "XPWidgets.h"

namespace {

const int kLocalListenPort = 49120;      // Plugin receives agent replies
const int kBridgeTargetPort = 49121;     // Bridge receives pilot prompts
const char* kBridgeTargetIp = "127.0.0.1";
const int kMaxMessageChars = 400;
const std::size_t kMaxHistory = 80;
const float kPollIntervalSec = 0.05f;
const XPLMFontID kChatFont = xplmFont_Proportional;
const float kChatTextScale = 1.7f;

XPLMMenuID gMenuContainer = nullptr;
int gMenuItem = 0;
XPLMMenuID gMenu = nullptr;
XPWidgetID gWindow = nullptr;
XPWidgetID gInputField = nullptr;
XPWidgetID gSendButton = nullptr;

socket_t gSocketFd = INVALID_SOCKET;
sockaddr_in gBridgeAddr {};

std::deque<std::string> gHistory;
int gChatLineHeight = 18;

static void draw_scaled_string(float color[3], int x, int y, const std::string& text) {
    glPushMatrix();
    glTranslatef(static_cast<float>(x), static_cast<float>(y), 0.0f);
    glScalef(kChatTextScale, kChatTextScale, 1.0f);
    XPLMDrawString(color, 0, 0, const_cast<char*>(text.c_str()), nullptr, kChatFont);
    glPopMatrix();
}

static std::string trim_copy(const std::string& s) {
    std::size_t start = 0;
    while (start < s.size() && std::isspace(static_cast<unsigned char>(s[start])) != 0) {
        ++start;
    }
    std::size_t end = s.size();
    while (end > start && std::isspace(static_cast<unsigned char>(s[end - 1])) != 0) {
        --end;
    }
    return s.substr(start, end - start);
}

static std::string sanitize_single_line(const std::string& raw) {
    std::string out;
    out.reserve(raw.size());
    for (char c : raw) {
        if (c == '\r' || c == '\n' || c == '\t') {
            out.push_back(' ');
            continue;
        }
        unsigned char uc = static_cast<unsigned char>(c);
        if (uc < 32 && c != ' ') {
            continue;
        }
        out.push_back(c);
        if (static_cast<int>(out.size()) >= kMaxMessageChars) {
            break;
        }
    }
    return trim_copy(out);
}

static std::vector<std::string> wrap_text_by_columns(
    const std::string& text,
    int maxColumns) {
    std::vector<std::string> lines;
    std::string source = trim_copy(text);
    if (source.empty()) {
        lines.push_back("");
        return lines;
    }

    auto utf8_char_len = [](unsigned char c) -> int {
        if ((c & 0x80u) == 0u) return 1;
        if ((c & 0xE0u) == 0xC0u) return 2;
        if ((c & 0xF0u) == 0xE0u) return 3;
        if ((c & 0xF8u) == 0xF0u) return 4;
        return 1;
    };

    std::string current;
    int col = 0;
    std::size_t i = 0;
    while (i < source.size()) {
        unsigned char c = static_cast<unsigned char>(source[i]);
        int n = utf8_char_len(c);
        if (i + static_cast<std::size_t>(n) > source.size()) {
            n = 1;
        }
        std::string ch = source.substr(i, static_cast<std::size_t>(n));
        int w = (n == 1 ? 1 : 2);  // Non-ASCII treated as double-width.

        if (col + w > maxColumns && !current.empty()) {
            lines.push_back(trim_copy(current));
            current.clear();
            col = 0;
        }
        current += ch;
        col += w;
        i += static_cast<std::size_t>(n);
    }

    if (!current.empty()) {
        lines.push_back(trim_copy(current));
    }

    if (lines.empty()) {
        lines.push_back("");
    }
    return lines;
}

static void push_history(const std::string& line) {
    if (line.empty()) {
        return;
    }
    gHistory.push_back(line);
    while (gHistory.size() > kMaxHistory) {
        gHistory.pop_front();
    }
}

static std::string get_widget_text(XPWidgetID widget) {
    if (widget == nullptr) {
        return "";
    }
    int len = XPGetWidgetDescriptor(widget, nullptr, 0);
    if (len <= 0) {
        return "";
    }
    std::vector<char> buf(static_cast<std::size_t>(len) + 1, '\0');
    XPGetWidgetDescriptor(widget, buf.data(), static_cast<int>(buf.size()));
    return std::string(buf.data());
}

static void set_widget_text(XPWidgetID widget, const std::string& text) {
    if (widget == nullptr) {
        return;
    }
    XPSetWidgetDescriptor(widget, text.c_str());
}

static bool udp_open() {
#ifdef IBM
    WSADATA wsa {};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        return false;
    }
#endif
    gSocketFd = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (gSocketFd == INVALID_SOCKET) {
        return false;
    }

    sockaddr_in localAddr {};
    localAddr.sin_family = AF_INET;
    localAddr.sin_addr.s_addr = htonl(INADDR_ANY);
    localAddr.sin_port = htons(static_cast<unsigned short>(kLocalListenPort));
    if (::bind(gSocketFd, reinterpret_cast<sockaddr*>(&localAddr), sizeof(localAddr)) == SOCKET_ERROR) {
        return false;
    }

#ifdef IBM
    u_long nonBlocking = 1;
    ioctlsocket(gSocketFd, FIONBIO, &nonBlocking);
#else
    int flags = fcntl(gSocketFd, F_GETFL, 0);
    fcntl(gSocketFd, F_SETFL, flags | O_NONBLOCK);
#endif

    std::memset(&gBridgeAddr, 0, sizeof(gBridgeAddr));
    gBridgeAddr.sin_family = AF_INET;
    gBridgeAddr.sin_port = htons(static_cast<unsigned short>(kBridgeTargetPort));
#ifdef IBM
    inet_pton(AF_INET, kBridgeTargetIp, &gBridgeAddr.sin_addr);
#else
    gBridgeAddr.sin_addr.s_addr = inet_addr(kBridgeTargetIp);
#endif
    return true;
}

static void udp_close() {
    if (gSocketFd == INVALID_SOCKET) {
        return;
    }
#ifdef IBM
    closesocket(gSocketFd);
    WSACleanup();
#else
    close(gSocketFd);
#endif
    gSocketFd = INVALID_SOCKET;
}

static void udp_send(const std::string& wire) {
    if (gSocketFd == INVALID_SOCKET) {
        return;
    }
    if (wire.empty()) {
        return;
    }
    ::sendto(
        gSocketFd,
        wire.c_str(),
        static_cast<int>(wire.size()),
        0,
        reinterpret_cast<sockaddr*>(&gBridgeAddr),
        sizeof(gBridgeAddr));
}

static void send_pilot_message(const std::string& pilotText) {
    std::string cleaned = sanitize_single_line(pilotText);
    if (cleaned.empty()) {
        return;
    }
    push_history("PILOT> " + cleaned);
    udp_send("PILOT|" + cleaned);
}

static float poll_bridge_cb(float, float, int, void*) {
    if (gSocketFd == INVALID_SOCKET) {
        return kPollIntervalSec;
    }

    char buffer[2048];
    sockaddr_in src {};
    socket_len_t srcLen = sizeof(src);
    while (true) {
        int n = recvfrom(
            gSocketFd,
            buffer,
            sizeof(buffer) - 1,
            0,
            reinterpret_cast<sockaddr*>(&src),
            &srcLen);
        if (n == SOCKET_ERROR || n <= 0) {
            break;
        }
        buffer[n] = '\0';
        std::string msg = sanitize_single_line(std::string(buffer));
        if (msg.empty()) {
            continue;
        }
        if (msg.rfind("AGENT|", 0) == 0) {
            push_history("AGENT> " + msg.substr(6));
        } else if (msg.rfind("SYSTEM|", 0) == 0) {
            push_history("SYSTEM> " + msg.substr(7));
        } else {
            push_history("RX> " + msg);
        }
    }
    return kPollIntervalSec;
}

static int chat_draw_cb(XPLMDrawingPhase, int, void*) {
    if (gWindow == nullptr || XPIsWidgetVisible(gWindow) == 0) {
        return 1;
    }

    int left = 0;
    int top = 0;
    int right = 0;
    int bottom = 0;
    XPGetWidgetGeometry(gWindow, &left, &top, &right, &bottom);

    const int panelLeft = left + 8;
    const int panelRight = right - 8;
    const int panelTop = top - 50;
    const int panelBottom = bottom + 8;
    XPLMDrawTranslucentDarkBox(panelLeft, panelTop, panelRight, panelBottom);
    static float color[3] = {0.8f, 0.95f, 0.8f};
    draw_scaled_string(color, left + 12, top - 34, "Agent Chat");

    std::vector<std::string> flattened;
    const int panelWidth = std::max((panelRight - panelLeft) - 16, 100);
    const int maxColumns = std::max(static_cast<int>(panelWidth / (8.0f * kChatTextScale)), 16);
    for (const std::string& row : gHistory) {
        std::vector<std::string> wrapped = wrap_text_by_columns(row, maxColumns);
        flattened.insert(flattened.end(), wrapped.begin(), wrapped.end());
    }

    int y = top - 54;
    const int maxLines = std::max((panelTop - panelBottom - 8) / gChatLineHeight, 4);
    int start = static_cast<int>(flattened.size()) - maxLines;
    if (start < 0) {
        start = 0;
    }

    for (int i = start; i < static_cast<int>(flattened.size()); ++i) {
        std::string line = flattened[static_cast<std::size_t>(i)];
        draw_scaled_string(color, left + 12, y, line);
        y -= gChatLineHeight;
    }
    return 1;
}

static void toggle_window_visibility() {
    if (gWindow == nullptr) {
        return;
    }
    if (XPIsWidgetVisible(gWindow)) {
        XPHideWidget(gWindow);
    } else {
        XPShowWidget(gWindow);
        XPBringRootWidgetToFront(gWindow);
        if (gInputField != nullptr) {
            XPSetKeyboardFocus(gInputField);
        }
    }
}

static int chat_widget_cb(XPWidgetMessage inMessage, XPWidgetID inWidget, intptr_t inParam1, intptr_t) {
    if (inMessage == xpMsg_PushButtonPressed && reinterpret_cast<XPWidgetID>(inParam1) == gSendButton) {
        std::string text = get_widget_text(gInputField);
        send_pilot_message(text);
        set_widget_text(gInputField, "");
        XPSetKeyboardFocus(gInputField);
        return 1;
    }
    if (inMessage == xpMsg_KeyPress && inWidget == gInputField) {
        XPKeyState_t* ks = reinterpret_cast<XPKeyState_t*>(inParam1);
        if (ks != nullptr && ks->key == '\r' && (ks->flags & xplm_DownFlag) != 0) {
            std::string text = get_widget_text(gInputField);
            send_pilot_message(text);
            set_widget_text(gInputField, "");
            XPSetKeyboardFocus(gInputField);
            return 1;
        }
    }
    if (inMessage == xpMessage_CloseButtonPushed && reinterpret_cast<XPWidgetID>(inParam1) == gWindow) {
        XPHideWidget(gWindow);
        return 1;
    }
    return 0;
}

static void create_ui() {
    if (gWindow != nullptr) {
        return;
    }
    gWindow = XPCreateWidget(
        80, 760, 980, 280,
        1,
        "Pilot-Agent Chat",
        1,
        nullptr,
        xpWidgetClass_MainWindow);
    XPSetWidgetProperty(gWindow, xpProperty_MainWindowType, xpMainWindowStyle_Translucent);
    XPSetWidgetProperty(gWindow, xpProperty_MainWindowHasCloseBoxes, 1);
    XPAddWidgetCallback(gWindow, chat_widget_cb);

    gInputField = XPCreateWidget(
        100, 320, 840, 286,
        1,
        "",
        0,
        gWindow,
        xpWidgetClass_TextField);
    XPSetWidgetProperty(gInputField, xpProperty_TextFieldType, xpTextEntryField);
    XPSetWidgetProperty(gInputField, xpProperty_MaxCharacters, kMaxMessageChars);
    XPAddWidgetCallback(gInputField, chat_widget_cb);

    gSendButton = XPCreateWidget(
        850, 320, 960, 286,
        1,
        "Send",
        0,
        gWindow,
        xpWidgetClass_Button);
    XPSetWidgetProperty(gSendButton, xpProperty_ButtonType, xpPushButton);
    XPSetWidgetProperty(gSendButton, xpProperty_ButtonBehavior, xpButtonBehaviorPushButton);

    push_history("SYSTEM> Chat plugin loaded. Start bridge on UDP 49121.");
}

static void destroy_ui() {
    if (gWindow != nullptr) {
        XPDestroyWidget(gWindow, 1);
        gWindow = nullptr;
    }
    gInputField = nullptr;
    gSendButton = nullptr;
}

static void menu_handler(void*, void*) {
    toggle_window_visibility();
}

static void setup_menu() {
    gMenuContainer = XPLMFindPluginsMenu();
    gMenuItem = XPLMAppendMenuItem(gMenuContainer, "Agent Chat", nullptr, 0);
    gMenu = XPLMCreateMenu("Agent Chat", gMenuContainer, gMenuItem, menu_handler, nullptr);
    XPLMAppendMenuItem(gMenu, "Show/Hide Chat", nullptr, 0);
}

static void teardown_menu() {
    if (gMenu != nullptr) {
        XPLMDestroyMenu(gMenu);
        gMenu = nullptr;
    }
}

}  // namespace

PLUGIN_API int XPluginStart(char* outName, char* outSig, char* outDesc) {
    std::strncpy(outName, "Agent Chat Plugin", 255);
    outName[255] = '\0';
    std::strncpy(outSig, "co.limbo.agentchat", 255);
    outSig[255] = '\0';
    std::strncpy(outDesc, "Chat UI between pilot and agent over localhost UDP.", 255);
    outDesc[255] = '\0';
    setup_menu();
    return 1;
}

PLUGIN_API void XPluginStop() {
    XPLMUnregisterDrawCallback(chat_draw_cb, xplm_Phase_Window, 0, nullptr);
    XPLMUnregisterFlightLoopCallback(poll_bridge_cb, nullptr);
    destroy_ui();
    udp_close();
    teardown_menu();
}

PLUGIN_API void XPluginDisable() {
    XPLMUnregisterDrawCallback(chat_draw_cb, xplm_Phase_Window, 0, nullptr);
    XPLMUnregisterFlightLoopCallback(poll_bridge_cb, nullptr);
    destroy_ui();
    udp_close();
}

PLUGIN_API int XPluginEnable() {
    create_ui();
    int charWidth = 0;
    int charHeight = 0;
    XPLMGetFontDimensions(kChatFont, &charWidth, &charHeight, nullptr);
    gChatLineHeight = charHeight > 0
        ? static_cast<int>((charHeight + 6) * kChatTextScale)
        : static_cast<int>(20 * kChatTextScale);
    if (!udp_open()) {
        push_history("SYSTEM> UDP init failed.");
    } else {
        push_history("SYSTEM> UDP ready: local 49120 <-> bridge 49121.");
    }
    XPLMRegisterFlightLoopCallback(poll_bridge_cb, kPollIntervalSec, nullptr);
    XPLMRegisterDrawCallback(chat_draw_cb, xplm_Phase_Window, 0, nullptr);
    return 1;
}

PLUGIN_API void XPluginReceiveMessage(XPLMPluginID, int, void*) {
}
