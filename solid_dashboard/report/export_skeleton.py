import ast
from pathlib import Path


class SkeletonTransformer(ast.NodeTransformer):
    """AST-трансформер, который удаляет тела функций и методов."""
    
    def clear_body(self, node):
        doc = ast.get_docstring(node)
        # Если есть документация (docstring), оставляем ее. Иначе ставим pass.
        if doc:
            node.body = [ast.Expr(value=ast.Constant(value=doc))]
        else:
            node.body = [ast.Pass()]
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        return self.clear_body(node)

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        return self.clear_body(node)

def generate_project_mask(root_dir: str, output_file: str):
    root_path = Path(root_dir)
    
    with open(output_file, 'w', encoding='utf-8') as out:
        for py_file in root_path.rglob('*.py'):
            # Пропускаем виртуальное окружение и файлы миграций
            if '.venv' in py_file.parts or 'alembic' in py_file.parts or py_file.name == 'export_skeleton.py':
                continue
                
            try:
                code = py_file.read_text(encoding='utf-8')
                tree = ast.parse(code)
                
                # Применяем трансформацию (удаляем логику)
                SkeletonTransformer().visit(tree)
                skeleton_code = ast.unparse(tree)
                
                out.write(f"\n{'='*60}\n")
                out.write(f"FILE: {py_file.relative_to(root_path)}\n")
                out.write(f"{'='*60}\n")
                out.write(skeleton_code)
                out.write("\n")
                
            except Exception as e:
                out.write(f"\n# Parsing error {py_file}: {e}\n")

if __name__ == "__main__":
    generate_project_mask(
        root_dir=".", 
        output_file="docs/scopus_project_mask.txt"
    )
    print("Success! Mask saved in docs/scopus_project_mask.txt")

