import ghidra.app.script.GhidraScript;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

/**
 * Dumps every symbol the language declares, so the pspec SFR map can be asserted against
 * doc4235 Tables 3-1..3-11 through Ghidra's own symbol resolution rather than by grepping XML.
 * One line per symbol: "SYM <space>:<hex offset> <name> primary=<bool>".
 */
public class Symbols51 extends GhidraScript {
    @Override
    public void run() throws Exception {
        SymbolIterator it = currentProgram.getSymbolTable().getAllSymbols(true);
        while (it.hasNext()) {
            Symbol s = it.next();
            String space = s.getAddress().getAddressSpace().getName();
            String off = Long.toString(s.getAddress().getOffset(), 16);
            println("SYM " + space + ":" + off + " " + s.getName() + " primary=" + s.isPrimary());
        }
    }
}
