cmake_minimum_required(VERSION 3.16)
project(XPlaneAgentChatPlugin)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(PLUGIN_NAME "AgentChat")
set(PLUGIN_TARGET "agent_chat")

set(SDK_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.xpc_ref/xpcPlugin/SDK/CHeaders")

include_directories(
    "${SDK_ROOT}/XPLM"
    "${SDK_ROOT}/Widgets"
)

if(WIN32)
    add_definitions(-DIBM=1 -DXPLM200=1)
elseif(APPLE)
    add_definitions(-DAPL=1 -DXPLM200=1)
else()
    add_definitions(-DLIN=1 -DXPLM200=1)
endif()

add_library(${PLUGIN_TARGET} SHARED
    src/AgentChatPlugin.cpp
)

set_target_properties(${PLUGIN_TARGET} PROPERTIES
    PREFIX ""
    SUFFIX ".xpl"
    OUTPUT_NAME "win"
)

if(WIN32)
    target_link_directories(${PLUGIN_TARGET} PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/../.xpc_ref/xpcPlugin/SDK/Libraries/Win"
    )
    if(CMAKE_SIZEOF_VOID_P EQUAL 8)
        target_link_libraries(${PLUGIN_TARGET} PRIVATE XPLM_64 XPWidgets_64)
    else()
        target_link_libraries(${PLUGIN_TARGET} PRIVATE XPLM XPWidgets)
    endif()
    target_link_libraries(${PLUGIN_TARGET} PRIVATE ws2_32 OpenGL32)
endif()

set(PLUGIN_OUTPUT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/build_output/${PLUGIN_NAME}/64")
set_target_properties(${PLUGIN_TARGET} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${PLUGIN_OUTPUT_DIR}"
    LIBRARY_OUTPUT_DIRECTORY "${PLUGIN_OUTPUT_DIR}"
)
