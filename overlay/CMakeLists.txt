cmake_minimum_required(VERSION 3.16)
project(motion-dots-overlay CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

find_package(GLEW REQUIRED)
find_package(glfw3 3.3 REQUIRED)
find_package(OpenGL REQUIRED)
find_package(X11 REQUIRED)

add_executable(motiondots-overlay main.cpp)

target_include_directories(motiondots-overlay PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${GLEW_INCLUDE_DIRS}
    ${X11_INCLUDE_DIR}
    ${OPENGL_INCLUDE_DIR})

target_link_libraries(motiondots-overlay PRIVATE
    glfw
    GLEW::GLEW
    ${OPENGL_LIBRARIES}
    ${X11_LIBRARIES})

# Optional: an empty X input region so the overlay can never intercept a touch or
# click meant for the game. Absence only costs us that guarantee, not the overlay.
if(X11_Xfixes_FOUND)
    target_compile_definitions(motiondots-overlay PRIVATE MOTIONDOTS_HAVE_XFIXES)
    target_link_libraries(motiondots-overlay PRIVATE ${X11_Xfixes_LIB})
    message(STATUS "XFixes found: overlay will be click-through")
else()
    message(WARNING "XFixes not found: overlay will not set an empty input region")
endif()
