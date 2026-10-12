/**
 * Business Rule: "ML triage - predict on create"
 *   Table: incident     When: async     Insert: true     Update: false
 *   Async means the ticket is saved first; a slow or failed call never blocks the caller.
 *
 * One-time setup (see servicenow/README.md):
 *   - REST Message "ML Triage", HTTP method "predict":
 *       POST https://<triage-host>/predict
 *       header  X-API-Key: ${api_key}      header  Content-Type: application/json
 *   - System properties:
 *       x_triage.mode            off | shadow | suggest | auto     (start with shadow)
 *       x_triage.api_key         (type password2)
 *       x_triage.fallback_group  name of the fallback group (IMG-SMC-TCC-ITSC)
 *   - Incident fields: u_pred_ci, u_pred_ci_conf, u_pred_group, u_pred_group_conf,
 *       u_pred_priority, u_pred_subcategory, u_urgent_alert, u_ci_mismatch,
 *       u_ml_auto_routed, u_ml_model_version
 *   - Event x_triage.urgent_alert (+ a notification on it for the duty manager)
 *
 * Values are sent as DISPLAY values ("1 - High", CI names, "Email") because the
 * model was trained on a list export, which holds display values. If you
 * retrain from an export with sys_ids, send sys_ids here instead.
 */
(function executeRule(current, previous) {
    var mode = gs.getProperty('x_triage.mode', 'shadow');
    if (mode == 'off')
        return;

    var groupAtInsert = current.getValue('assignment_group');
    var ticket = {
        number: current.getValue('number'),
        opened_at: current.getValue('opened_at'),
        short_description: current.getValue('short_description') || '',
        description: current.getValue('description') || '',
        contact_type: current.getDisplayValue('contact_type'),
        caller_id: current.getDisplayValue('caller_id'),
        sys_created_by: current.getValue('sys_created_by'),
        impact: current.getDisplayValue('impact'),
        cmdb_ci: current.cmdb_ci.nil() ? null : current.getDisplayValue('cmdb_ci')
    };

    var r;
    try {
        var rm = new sn_ws.RESTMessageV2('ML Triage', 'predict');
        rm.setStringParameterNoEscape('api_key', gs.getProperty('x_triage.api_key'));
        rm.setRequestBody(JSON.stringify({ tickets: [ticket] }));
        rm.setHttpTimeout(5000);
        var resp = rm.execute();
        if (resp.getStatusCode() != 200) {
            gs.warn('ML triage: HTTP ' + resp.getStatusCode() + ' for ' + ticket.number);
            return; // ticket follows the normal manual process
        }
        var body = JSON.parse(resp.getBody());
        r = body.results[0];
        r.model_version = body.model_version;
    } catch (e) {
        gs.warn('ML triage: call failed for ' + ticket.number + ': ' + e);
        return;
    }

    var inc = new GlideRecord('incident');
    if (!inc.get(current.getUniqueValue()))
        return;

    // Always record the prediction (shadow mode = only this).
    inc.u_pred_ci = r.pred_cmdb_ci || '';
    inc.u_pred_ci_conf = r.conf_cmdb_ci || 0;
    inc.u_pred_group = r.pred_assignment_group || '';
    inc.u_pred_group_conf = r.conf_assignment_group || 0;
    inc.u_pred_priority = r.pred_priority || '';
    inc.u_pred_subcategory = r.pred_subcategory || '';
    inc.u_urgent_alert = !!r.urgent_alert;
    inc.u_ci_mismatch = !!r.ci_mismatch;
    inc.u_ml_model_version = r.model_version;

    var changedRouting = false;
    if (mode == 'auto') {
        // Do not overwrite a person: only route if the group is unchanged since insert.
        var untouched = inc.getValue('assignment_group') == groupAtInsert;
        if (r.auto_assignment_group && untouched) {
            var grp = lookup('sys_user_group', r.pred_assignment_group);
            if (grp) {
                inc.assignment_group = grp;
                inc.u_ml_auto_routed = true;
                changedRouting = true;
            }
        }
        // Fill the CI only when the requester left it empty; a disagreement is just flagged.
        if (r.auto_cmdb_ci && inc.cmdb_ci.nil()) {
            var ci = lookup('cmdb_ci', r.pred_cmdb_ci);
            if (ci)
                inc.cmdb_ci = ci;
        }
        if (changedRouting) {
            inc.work_notes = 'ML triage (' + r.model_version + '): routed to ' + r.pred_assignment_group +
                ' (confidence ' + r.conf_assignment_group + '). Reassign as normal if this is wrong.';
        }
    }

    // Only run other business rules / notifications when routing really changed.
    inc.setWorkflow(changedRouting);
    inc.update();

    if (r.urgent_alert && mode != 'shadow')
        gs.eventQueue('x_triage.urgent_alert', inc, r.pred_priority, String(r.urgency_score || ''));

    function lookup(table, name) {
        var gr = new GlideRecord(table);
        gr.addQuery('name', name);
        gr.addActiveQuery();
        gr.setLimit(1);
        gr.query();
        return gr.next() ? gr.getUniqueValue() : null;
    }
})(current, previous);
