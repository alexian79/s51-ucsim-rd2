// Disassemble the imported image and print each instruction with Ghidra's
// resolved operand symbols, so SFR naming and dual-DPTR handling can be compared
// across 8051 language variants.
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;

public class Dump51 extends GhidraScript {
    public void run() throws Exception {
        StringBuilder sp = new StringBuilder();
        for (AddressSpace as : currentProgram.getAddressFactory().getAddressSpaces()) {
            sp.append(as.getName()).append(" ");
        }
        println("LANG " + currentProgram.getLanguage().getLanguageID());
        println("SPACES " + sp);

        AddressSpace rom = currentProgram.getAddressFactory().getAddressSpace("ROM");
        if (rom == null) {
            rom = currentProgram.getAddressFactory().getDefaultAddressSpace();
        }

        // Force the whole loaded range to disassemble: with -noanalysis nothing is
        // decoded, and flow-only analysis skips fall-through tails.
        Address a = rom.getAddress(0);
        Address last = rom.getMaxAddress();
        while (a != null && a.compareTo(last) <= 0) {
            if (currentProgram.getListing().getInstructionAt(a) == null) {
                new DisassembleCommand(a, null, true).applyTo(currentProgram);
            }
            Instruction nx = currentProgram.getListing().getInstructionAfter(a);
            a = (nx == null) ? null : nx.getAddress();
        }

        InstructionIterator it =
                currentProgram.getListing().getInstructions(rom.getAddress(0), true);
        int n = 0;
        while (it.hasNext() && n < 40) {
            Instruction ins = it.next();
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < ins.getNumOperands(); i++) {
                for (Object o : ins.getOpObjects(i)) {
                    sb.append(o).append(" ");
                }
                if (i < ins.getNumOperands() - 1) {
                    sb.append(", ");
                }
            }
            println(String.format("%-10s %-32s | %s", ins.getAddress(),
                    ins.toString().replaceAll("\\s+", " "), sb));
            n++;
        }
        println("INSTRS " + n);
    }
}
