import java.util.*;
import java.time.LocalDate;

class Task {
    String title;
    boolean isCompleted;
    String deadline;
    String priority;

    public Task(String title, String deadline,String priority) {
        this.title = title;
        this.isCompleted = false;
        this.deadline = deadline;
        this.priority=priority;
    }
}

public class TaskManager {
    static ArrayList<Task> tasks = new ArrayList<>();

    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);

        while (true) {
            System.out.println("1 for Add Task ");
            System.out.println("2 for View Task ");
            System.out.println("3 for Mark Complete ");
            System.out.println("4 for Delete Task");
            System.out.println("5 for Exit ");

            int choice = sc.nextInt();
            sc.nextLine();

            switch (choice) {
                case 1:

                addTask(sc);
                    // System.out.println("Enter Task");
                    // String title = sc.nextLine();
                    // if (title.trim().isEmpty()) { // trim() : removes empty space
                    //     System.out.println("Task cannot be empty!");
                    //     break;
                    // }
                    // System.out.println("Enter deadline(YYYY-MM-DD): ");
                    
                    // String deadline = sc.nextLine();

                    // System.out.println("Enter Priority(High/Medium/Low):  ");
                    // String priority= sc.nextLine();

                    // try {
                    //     LocalDate.parse(deadline);         // Validate Date Format
                    //     tasks.add(new Task(title, deadline,priority));
                    //     System.out.println("Task added successfully");
                    // } catch (Exception e) {
                    //     System.out.println("Invalid date format! Please use YYYY-MM-DD");
                    // }

                    break;

                case 2:
                    viewTask();
                    // if (tasks.isEmpty()) {
                    //     System.out.println("No Tasks Available!");
                    // } else {
                    //         // Sorting Logic: Earliest -> Latest deadline

                    //         Collections.sort(tasks,new Comparator<Task>() {
                    //             public int compare(Task t1,Task t2){
                    //                 LocalDate d1=LocalDate.parse(t1.deadline);
                    //                 LocalDate d2=LocalDate.parse(t2.deadline);

                    //                 return d1.compareTo(d2);

                    //             }
                                
                    //         });





                    //     for (int i = 0; i < tasks.size(); i++) {
                    //         Task t = tasks.get(i);

                    //         LocalDate today = LocalDate.now();
                    //         LocalDate dueDate = LocalDate.parse(t.deadline);

                    //         String status = tasks.get(i).isCompleted ? "Done" : "Pending";

                    //         if (!t.isCompleted && dueDate.isBefore(today)) {
                    //             status = "Overdue";
                    //         }
                    //         System.out.println((i+1) + ":" + tasks.get(i).title + " |" + " Deadline(YYYY-MM-DD):" + t.deadline + " |" + " Priority: "+t.priority + " |" + " Status: " + "["
                    //                 + status + "]");
                    //     }
                    // }
                    break;

                case 3:
                    if (tasks.isEmpty()) {
                        System.out.println("No tasks available to mark!");
                    } else {
                        System.out.print("Enter Index:");
                        int index = sc.nextInt();
                        index=index-1;
                        if (index >= 0 && index < tasks.size()) {
                            tasks.get(index).isCompleted = true;
                            System.out.println("Task " + (index+1) + " is mark Completed");
                        } else {
                            System.out.println("Invalid Index");
                        }

                    }
                    break;

                case 4:
                    if (tasks.isEmpty()) {
                        System.out.println("No tasks available to delete");
                    } else {
                        System.out.print("Enter the index:");
                        int ind = sc.nextInt();
                        ind=ind-1;
                        if (ind >= 0 && ind < tasks.size()) {
                            tasks.remove(ind);
                            System.out.println("Task " + (ind+1) + " deleted");
                        } else {
                            System.out.println("Invalid Index");
                        }

                    }

                    break;

                case 5:
                    return;

            }

        }

    }

public static void addTask(Scanner sc){
    System.out.println("Enter Task");
    String title = sc.nextLine();
    if(title.trim().isEmpty()){                   // trim() - removes empty space 
        System.out.println("Title cannot be Empty!");
        return;
    }
  System.out.println("Enter deadline(YYYY-MM-DD): ");   
  String deadline =   sc.nextLine();

  System.out.println("Enter Priority(High/Medium/Low): ");
  String priority = sc.nextLine();

  try{
    LocalDate.parse(deadline);       // Validate Date Format
    tasks.add(new Task(title,deadline, priority));
    System.out.println("Task added successfully");
} 
catch(Exception e){
    System.out.println("Invalid date format! Please use YYYY-MM-DD");
}
}

public static void viewTask(){
    if(tasks.isEmpty()){
        System.out.println("No Task Available");
    }
    else {
         // Sorting Logic: Earliest to Latest deadline
         Collections.sort(tasks,new Comparator<Task>() {
            public int compare(Task t1,Task t2){
                LocalDate d1= LocalDate.parse(t1.deadline);
                 LocalDate d2= LocalDate.parse(t2.deadline);

                 return d1.compareTo(d2);

            }
         });

         for(int i=0;i<tasks.size();i++){
            Task t= tasks.get(i);

            LocalDate today= LocalDate.now();
            LocalDate duDate= LocalDate.parse(t.deadline);

            String status =  tasks.get(i).isCompleted ? "Done" : "Pending";

            if(!t.isCompleted && duDate.isBefore(today)) {
                status= "Overdue";
            }
            System.out.println((i+1) + ":" + tasks.get(i).title + " |" + " Deadline(YYYY-MM-DD):" +t.deadline + " |" + " Priority: "+t.priority + " |" + " Status" +"[" + status +"]");
         }

         
    }

}






}
