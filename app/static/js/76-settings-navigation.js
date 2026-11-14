// 설정 메뉴 전환은 폼을 다시 만들지 않으므로 미저장 입력과 예약 값이 유지된다.
const SETTINGS_SECTION_KEY = "activeSettingsSection";
const settingsSectionTabs = Array.from(document.querySelectorAll(".settings-section-tab"));

function activateSettingsSection(name, { focus = false } = {}) {
  const target = settingsSectionTabs.find((tab) => tab.dataset.settingsSection === name) || settingsSectionTabs[0];
  if (!target) return;
  const selected = target.dataset.settingsSection;
  settingsSectionTabs.forEach((tab) => {
    const active = tab === target;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".settings-section-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.settingsPanel !== selected);
  });
  sessionStorage.setItem(SETTINGS_SECTION_KEY, selected);
  if (focus) target.focus();
}

settingsSectionTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activateSettingsSection(tab.dataset.settingsSection));
  tab.addEventListener("keydown", (event) => {
    let next = null;
    if (event.key === "ArrowRight") next = (index + 1) % settingsSectionTabs.length;
    else if (event.key === "ArrowLeft") next = (index + settingsSectionTabs.length - 1) % settingsSectionTabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = settingsSectionTabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    activateSettingsSection(settingsSectionTabs[next].dataset.settingsSection, { focus: true });
  });
});

activateSettingsSection(sessionStorage.getItem(SETTINGS_SECTION_KEY));
