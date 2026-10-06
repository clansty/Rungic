# Settings -> Services (docs/83): the KCM that lists the services the container keeps off, and
# the KAuth helper that turns them on or off as root.
cmake_minimum_required(VERSION 3.22)
project(rungic-services LANGUAGES CXX)

find_package(ECM 6.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 6.8 REQUIRED COMPONENTS Core DBus Network Quick Qml)
find_package(KF6 6.0 REQUIRED COMPONENTS KCMUtils Auth CoreAddons I18n)

kcmutils_add_qml_kcm(kcm_rungic_services SOURCES servicessettings.cpp)
target_link_libraries(kcm_rungic_services PRIVATE
    Qt::Core Qt::DBus Qt::Network Qt::Quick Qt::Qml
    KF6::KCMUtilsQuick KF6::CoreAddons KF6::AuthCore KF6::I18n)
# The page's QML is loaded with the plugin id as its translation domain (KQuickConfigModule).
target_compile_definitions(kcm_rungic_services PRIVATE TRANSLATION_DOMAIN="kcm_rungic_services")

add_executable(rungic-services-helper serviceshelper.cpp)
target_link_libraries(rungic-services-helper Qt::Core KF6::AuthCore)
install(TARGETS rungic-services-helper DESTINATION ${KAUTH_HELPER_INSTALL_DIR})
kauth_install_helper_files(rungic-services-helper com.rungic.services root)
kauth_install_actions(com.rungic.services com.rungic.services.actions)

# po/zh_CN/kcm_rungic_services.po: the page, its C++ and the texts of service-policy.json.
ki18n_install(po)
