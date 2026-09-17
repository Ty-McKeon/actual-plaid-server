from flask import Flask

def create_app():
    app = Flask(__name__)
    return app

app = create_app()

@app.route("/")
def dashboard():
    return "<h1>Hello World!</h1>"

if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")