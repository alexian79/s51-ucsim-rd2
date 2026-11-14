// Show how a 8051 language models the AT89C51RD2's variant features: SFR symbols,
// the decompiler's view of dual-DPTR selection, and the upper-128 IRAM aliasing.
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

public class Probe51 extends GhidraScript {
    public void run() throws Exception {
        StringBuilder sp = new StringBuilder();
        for (ghidra.program.model.address.AddressSpace as
                : currentProgram.getAddressFactory().getAddressSpaces()) {
            sp.append(as.getName()).append("=").append(as.getSize()).append(" ");
        }
        println("LANG " + currentProgram.getLanguage().getLanguageID());
        println("SPACES " + sp);

        // every named symbol in the SFR space: this is the language's register map
        StringBuilder syms = new StringBuilder();
        SymbolIterator sit = currentProgram.getSymbolTable().getSymbolIterator();
        int ns = 0;
        while (sit.hasNext()) {
            Symbol s = sit.next();
            if (s.getAddress().getAddressSpace().getName().equals("SFR")) {
                syms.append(s.getName()).append("@").append(s.getAddress().toString()).append(" ");
                ns++;
            }
        }
        println("SFR_SYMBOLS " + ns + " " + syms);

        StringBuilder regs = new StringBuilder();
        for (ghidra.program.model.lang.Register r : currentProgram.getLanguage().getRegisters()) {
            regs.append(r.getName()).append(" ");
        }
        println("REGISTERS " + regs);

        // no analysis pass ran, so create the entry function explicitly
        new ghidra.app.cmd.function.CreateFunctionCmd(currentProgram.getMinAddress())
                .applyTo(currentProgram);

        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        FunctionIterator fs = currentProgram.getFunctionManager().getFunctions(true);
        while (fs.hasNext()) {
            Function f = fs.next();
            DecompileResults r = dec.decompileFunction(f, 60, monitor);
            println("FN " + f.getName() + " @ " + f.getEntryPoint() + " decompiled="
                    + r.decompileCompleted());
            if (r.decompileCompleted()) {
                for (String ln : r.getDecompiledFunction().getC().split("\n")) {
                    println("  | " + ln);
                }
            } else {
                println("  | " + r.getErrorMessage());
            }
        }
        dec.dispose();
    }
}
