import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.pcode.PcodeOp;
import ghidra.program.model.pcode.Varnode;

/** Prints the pcode Ghidra generated for each instruction, for language-semantics checks. */
public class Pcode51 extends GhidraScript {
    @Override
    public void run() throws Exception {
        MemoryBlock[] bl = currentProgram.getMemory().getBlocks();
        for (int b = 0; b < bl.length; b++) {
            if (bl[b].isExecute() && bl[b].isInitialized()
                    && currentProgram.getListing().getInstructionAt(bl[b].getStart()) == null) {
                runCommand(new DisassembleCommand(bl[b].getStart(), null, true));
            }
        }
        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext()) {
            Instruction in = it.next();
            StringBuilder sb = new StringBuilder();
            sb.append(in.getAddress()).append("  ").append(in.toString()).append("   <<");
            PcodeOp[] ops = in.getPcode();
            for (int i = 0; i < ops.length; i++) {
                sb.append(' ').append(ops[i].getMnemonic().toUpperCase());
                sb.append('(').append(vs(ops[i].getInputs())).append(')');
                Varnode out = ops[i].getOutput();
                if (out != null && !out.isConstant()) {
                    sb.append("->").append(one(out));
                }
            }
            println(sb.toString());
        }
    }

    private String one(Varnode v) {
        if (v.isConstant()) {
            return Long.toHexString(v.getOffset());
        }
        Register r = currentProgram.getLanguage().getRegister(v.getAddress(), v.getSize());
        return (r == null ? v.getAddress().getAddressSpace().getName() : r.getName())
            + ":" + v.getAddress();
    }

    private String vs(Varnode[] v) {
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < v.length; i++) {
            if (i > 0) {
                s.append(',');
            }
            s.append(one(v[i]));
        }
        return s.toString();
    }
}
