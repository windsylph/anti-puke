cmake_minimum_required(VERSION 3.16)
project(motion-dots-sensor-service CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# hidapi (hidraw backend). Upstream's Makefile also pulled in ncurses and
# libsystemd; the trimmed service needs neither.
find_package(PkgConfig REQUIRED)
pkg_check_modules(HIDAPI REQUIRED hidapi-hidraw)

file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS src/*.cpp)

add_executable(motiondots-sensord ${SOURCES})

target_include_directories(motiondots-sensord PRIVATE inc ${HIDAPI_INCLUDE_DIRS})
target_link_directories(motiondots-sensord PRIVATE ${HIDAPI_LIBRARY_DIRS})
target_link_libraries(motiondots-sensord PRIVATE ${HIDAPI_LIBRARIES} pthread)
