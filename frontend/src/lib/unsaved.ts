// Guard against leaving a page with unsaved edits.
//
// The app uses <BrowserRouter> (not a data router), so react-router's useBlocker
// isn't available — instead a page marks itself dirty here and the sidebar links
// ask for confirmation before navigating away.

let dirty = false;
let message = "Изменения не сохранены. Уверены, что хотите покинуть страницу?";

export function setUnsaved(v: boolean, msg?: string): void {
  dirty = v;
  if (msg) message = msg;
}

export function hasUnsaved(): boolean {
  return dirty;
}

/** True = it's ok to navigate. Shows a confirm only when there are edits. */
export function confirmLeave(): boolean {
  if (!dirty) return true;
  // eslint-disable-next-line no-alert
  return window.confirm(message);
}
