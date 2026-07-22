// Fixture source for the schema-inventory gate's rejection tests (#634). NOT
// compiled -- the comparator parses it as text (syn) and scans it for
// `.emit_json(` call sites. Each construct below drives exactly one violation
// variant so the gate is proven to reject each drift class by execution.

// Declared in the drifted index with a good schema -> no violation.
pub struct DeclaredGood;
impl crate::ui::CliOutput for DeclaredGood {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({})
    }
    fn render(&self, _ui: &crate::ui::Ui) {}
}

// An `impl CliOutput` the index never declares -> UndeclaredResult (the teeth).
pub struct UndeclaredOne;
impl CliOutput for UndeclaredOne {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({})
    }
    fn render(&self, _ui: &crate::ui::Ui) {}
}

// A raw `Ui::emit_json` result NOT in the `raw_emit_sites` allowlist ->
// UnexpectedRawEmitter.
pub fn emits_directly(ui: &crate::ui::Ui) {
    ui.emit_json(&serde_json::json!({ "undeclared_raw": true }));
}
