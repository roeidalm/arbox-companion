(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ArboxCancellation = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function message(session, preview) {
    const when = `${session.category_name || 'אימון'} · ${session.date} ${session.start_time}`;
    const fee = preview.late_cancel
      ? 'זהו ביטול מאוחר שייספר ככניסה במנוי.'
      : preview.standby ? 'המקום ברשימת ההמתנה ישוחרר.' : 'הביטול כעת בתוך חלון הביטול החופשי.';
    return `לבטל את ${when}?\n\n${fee}\nהמקום משתחרר לאחרים; חזרה לאימון תלויה בזמינות.`;
  }
  function validate(preview, session, studio) {
    if (preview?.studio_id !== studio || preview?.schedule_id !== session.schedule_id ||
        !Number.isInteger(preview.booking_id) || typeof preview.late_cancel !== 'boolean' ||
        preview.booking_id !== (preview.standby ? session.user_in_standby : session.user_booked))
      throw new Error('ההרשמה השתנתה. רעננו ופתחו את האימון מחדש');
    return preview;
  }
  return {message, validate};
});
