# Build the report: render Graphviz figures, then run pdflatex twice.
FIGS := $(patsubst figures/%.dot,figures/%.pdf,$(wildcard figures/*.dot))

all: report.pdf

figures/%.pdf: figures/%.dot
	dot -Tpdf $< -o $@

report.pdf: report.tex $(FIGS)
	pdflatex -interaction=nonstopmode -halt-on-error report.tex
	pdflatex -interaction=nonstopmode -halt-on-error report.tex

clean:
	rm -f report.aux report.log report.out report.toc report.bbl report.blg report.pdf $(FIGS)

.PHONY: all clean
