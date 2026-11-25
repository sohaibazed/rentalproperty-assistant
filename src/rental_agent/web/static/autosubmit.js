// Save a form as soon as one of its <select data-autosubmit> lists changes.
// A separate file because the site's Content-Security-Policy blocks inline scripts.
document.addEventListener("change", (event) => {
  if (event.target.matches("select[data-autosubmit]")) {
    event.target.form.requestSubmit();
  }
});
