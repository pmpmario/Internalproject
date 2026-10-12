/**
 * UI Action: "Accept ML suggestion"  (suggest mode)
 *   Table: incident   Form button: true   Action name: accept_ml_suggestion
 *   Condition: !current.u_pred_group.nil() && current.u_pred_group != current.assignment_group.getDisplayValue()
 *
 * Lets the agent apply the predicted team (and CI, if empty) in one click.
 * Count how often it is clicked: that acceptance rate is the evidence for auto mode.
 */
var grp = new GlideRecord('sys_user_group');
if (grp.get('name', current.getValue('u_pred_group'))) {
    current.assignment_group = grp.getUniqueValue();
}
if (current.cmdb_ci.nil() && !current.u_pred_ci.nil()) {
    var ci = new GlideRecord('cmdb_ci');
    if (ci.get('name', current.getValue('u_pred_ci')))
        current.cmdb_ci = ci.getUniqueValue();
}
current.work_notes = 'ML suggestion accepted (' + current.getValue('u_ml_model_version') + ').';
current.update();
action.setRedirectURL(current);
